"""The IrClient pipeline (plan §4): version resolve -> Router -> capability mapping -> validator
-> write policy / dry-run gate -> request builder -> AuthManager -> Transport.send -> normalizer,
with the ErrorMapper on every failure path and ``to_envelope`` at the edge.

``RestClient`` (exported as ``IrClient`` too) is the programmatic entry point: ``call`` takes an
operationId or a capabilityId. REST operations run here; SOAP operations are handed to
``SoapClient`` (M5), which shares the validator, write policy, preview ledger, ErrorMapper and
Redactor. Everything that leaves this module passes through the ``Redactor``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from imageright_mcp.catalog import Catalog, get_catalog
from imageright_mcp.client.auth import AuthError, AuthManager, AuthSettings, CommandRunner
from imageright_mcp.client.auth import run_command as default_runner
from imageright_mcp.client.builder import BuildError, RequestBuilder
from imageright_mcp.client.capability import map_params
from imageright_mcp.client.models import PreparedRequest, RawResponse, TransportFailure
from imageright_mcp.client.normalize import normalize, rest_type
from imageright_mcp.client.pipeline import CallOutcome, policy_gate
from imageright_mcp.client.policy import PreviewLedger
from imageright_mcp.client.redact import RedactingFilter, Redactor
from imageright_mcp.client.router import RouteError, Router
from imageright_mcp.client.soap import SoapClient
from imageright_mcp.client.transport import (
    BodySink,
    RestTransport,
    RetryPolicy,
    Sleep,
    Transport,
    send_with_retries,
)
from imageright_mcp.client.validator import Issue, Validator
from imageright_mcp.config import EffectiveConfig, strip_userinfo
from imageright_mcp.errors import ErrorContext, ErrorMapper, get_registry

logger = logging.getLogger(__name__)
PACKAGE_LOGGER = "imageright_mcp"

__all__ = ["CallOutcome", "RestClient"]


def _decode_body(response: RawResponse) -> Any:
    if response.file is not None:
        return response.file.to_dict()
    if not response.content:
        return None
    text = response.content.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


class RestClient:
    def __init__(
        self,
        config: EffectiveConfig,
        *,
        transport: Transport | None = None,
        catalog: Catalog | None = None,
        redactor: Redactor | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Sleep = asyncio.sleep,
        command_runner: CommandRunner = default_runner,
    ) -> None:
        self.config = config
        self.catalog = catalog or get_catalog()
        self.redactor = redactor or Redactor(config.secret_values())
        self.sink = BodySink(Path(config.outputDir).expanduser() if config.outputDir else None)
        # One HTTP client for both surfaces; SOAP adds its own serialized session on top.
        self.transport: Transport = transport or RestTransport(
            timeout=config.timeoutSeconds,
            verify=config.caBundle or config.verifyTls,
            request_id_header=config.requestIdHeader,
            sink=self.sink,
        )
        self.mapper = ErrorMapper(operations=self.catalog.ops)
        self.validator = Validator(self.catalog.schemas)
        self.router = Router(self.catalog)
        self.base_url = strip_userinfo(config.restBaseUrl)
        self.url_had_credentials = self.base_url != config.restBaseUrl
        self.builder = RequestBuilder(self.base_url, config.fileRoots)
        self.retry = RetryPolicy(max_retries=config.maxRetries)
        self.sleep = sleep
        self.ledger = PreviewLedger()
        self.auth = AuthManager(
            AuthSettings.from_config(config),
            transport=self.transport,
            base_url=self.base_url,
            operations=self.catalog.ops,
            redactor=self.redactor,
            mapper=self.mapper,
            clock=clock,
            command_runner=command_runner,
        )
        self.soap = SoapClient(
            config,
            transport=self.transport,
            catalog=self.catalog,
            redactor=self.redactor,
            mapper=self.mapper,
            validator=self.validator,
            ledger=self.ledger,
            retry=self.retry,
            sink=self.sink,
            clock=clock,
            sleep=sleep,
        )
        self._install_log_filter()

    def _install_log_filter(self) -> None:
        package_logger = logging.getLogger(PACKAGE_LOGGER)
        if not any(isinstance(f, RedactingFilter) for f in package_logger.filters):
            package_logger.addFilter(RedactingFilter(self.redactor))
        else:
            for f in package_logger.filters:
                if isinstance(f, RedactingFilter):
                    f.redactor = self.redactor

    def reconfigure(self, config: EffectiveConfig) -> None:
        """Apply settings that need no new connection or auth session (write mode, version,
        surface preference, file roots, retries); ``Runtime`` rebuilds the client otherwise."""
        self.config = config
        self.soap.config = config
        self.builder = RequestBuilder(self.base_url, config.fileRoots)
        self.retry = RetryPolicy(max_retries=config.maxRetries)
        self.soap.session.retry = self.retry

    def adopt_ledger(self, previous: RestClient) -> None:
        """Keep preview ids issued before a rebuild confirmable."""
        self.ledger = previous.ledger
        self.soap.ledger = previous.ledger

    async def aclose(self) -> None:
        """Log the SOAP session off (best effort), then close the HTTP client."""
        await self.soap.aclose()
        await self.transport.aclose()

    # ------------------------------------------------------------------ pipeline

    def enabled_surfaces(self) -> set[str] | None:
        """Surfaces with a configured endpoint; ``None`` when none is configured (routing then
        goes by catalog and preference only, which is what an offline dry-run needs)."""
        enabled: set[str] = set()
        if self.config.restBaseUrl:
            enabled |= {"rest-v1", "rest-v2"}
        if self.config.soapUrl:
            enabled.add("soap")
        return enabled or None

    async def call(
        self,
        ident: str,
        params: Mapping[str, Any] | None = None,
        files: Mapping[str, str] | None = None,
        *,
        dry_run: bool | None = None,
        confirm: str | None = None,
        surface: str | None = None,
        include_raw: bool = False,
        capability_id: str | None = None,
    ) -> CallOutcome:
        """Run one operationId or capabilityId through the whole pipeline (plan §4):
        route -> map -> validate -> policy / dry-run -> build -> auth -> send -> normalize."""
        params = dict(params or {})
        files = dict(files or {})
        registry = get_registry()
        profile = self.config.profile.profile
        meta: dict[str, Any] = {
            "irVersion": self.config.irVersion,
            "profile": profile,
            "operationId": ident,
            "capabilityId": capability_id,
            "dryRun": False,
            "warnings": [],
        }
        if profile is None:
            return self._finish(CallOutcome(error=registry.error("IR-1002"), meta=meta))
        capability = None
        if self.catalog.find_operation_id(ident) is None:
            capability = self.catalog.capabilities.get(ident.strip())
        supplied = set(params) | set(files)
        if capability is not None:
            supplied &= set(capability["params"])
        try:
            route = self.router.route(
                ident,
                profile=profile,
                preference=list(self.config.surfacePreference),
                force=surface,
                enabled=self.enabled_surfaces(),
                require_verified=self.config.requireVerifiedMappings,
                supplied=supplied,
            )
        except RouteError as exc:
            if exc.trail:
                meta["route"] = exc.trail
            return self._finish(CallOutcome(error=exc.error, meta=meta))
        op = self.catalog.ops[route.operation_id]
        meta["operationId"] = route.operation_id
        meta["capabilityId"] = route.capability_id or capability_id or op.get("capability")
        meta["surface"] = route.surface
        meta["route"] = route.trail
        meta["warnings"].extend(route.warnings)
        extra: list[Issue] = []
        if capability is not None and route.implementation is not None:
            try:
                mapped = map_params(
                    capability,
                    route.surface,
                    route.implementation,
                    op,
                    params,
                    files,
                    self.builder.file_part,
                )
            except BuildError as exc:
                return self._finish(CallOutcome(error=exc.error, meta=meta))
            params, files, extra = mapped.params, mapped.files, mapped.issues
            if mapped.notes:
                meta["notes"] = mapped.notes
        if op["surface"] == "soap":
            outcome = await self.soap.call(
                op,
                params,
                files,
                meta,
                dry_run=dry_run,
                confirm=confirm,
                route=route.trail,
                extra_issues=extra,
            )
        else:
            outcome = await self._rest_call(
                op, params, files, meta, route.trail, extra, dry_run=dry_run, confirm=confirm
            )
        return self._finish(
            self._normalize(outcome, op, canonical=capability is not None, include_raw=include_raw)
        )

    def _normalize(
        self, outcome: CallOutcome, op: Mapping[str, Any], *, canonical: bool, include_raw: bool
    ) -> CallOutcome:
        """Pipeline step 8 (plan §4.5) for a result that came back from the server."""
        if not outcome.ok or outcome.meta.get("dryRun") or "httpStatus" not in outcome.meta:
            return outcome
        surface = str(op["surface"])
        if surface == "soap":
            shape = outcome.meta.get("shape")
            type_name = str(shape).split(":", 1)[1] if shape else None
        elif op.get("method") == "HEAD":
            return outcome
        else:
            type_name = rest_type(op, outcome.meta.get("httpStatus"))
        try:
            data, extra = normalize(
                outcome.data,
                surface=surface,
                type_name=type_name,
                canonical=canonical,
                include_raw=include_raw,
            )
        except Exception as exc:  # a malformed upstream payload must not become IR-9001
            logger.warning("normalization failed for %s: %s", op["id"], type(exc).__name__)
            error = get_registry().error(
                "IR-9002",
                native={"surface": surface, "shape": type_name, "exception": type(exc).__name__},
            )
            return CallOutcome(error=error, meta=outcome.meta)
        outcome.data = data
        outcome.meta.update(extra)
        return outcome

    async def _rest_call(
        self,
        op: Mapping[str, Any],
        params: dict[str, Any],
        files: dict[str, str],
        meta: dict[str, Any],
        route: list[dict[str, Any]],
        extra_issues: list[Issue],
        *,
        dry_run: bool | None,
        confirm: str | None,
    ) -> CallOutcome:
        registry = get_registry()
        profile = str(meta["profile"])
        validation = self.validator.validate(op, profile, params, files)
        validation.issues[:0] = extra_issues
        meta["warnings"].extend(validation.warnings)
        try:
            request = self.builder.build(op, params, files)
        except BuildError as exc:
            return CallOutcome(error=exc.error, meta=meta)
        if self.url_had_credentials:
            meta["warnings"].append(
                registry.warning(
                    "IR-1001",
                    "restBaseUrl contains user:password@; it is ignored. Use the auth settings.",
                )
            )
        if self.config.restBaseUrl is None:
            meta["warnings"].append(
                registry.warning(
                    "IR-1003", "restBaseUrl is not set; the preview uses a placeholder."
                )
            )
        gated = policy_gate(
            config=self.config,
            ledger=self.ledger,
            redactor=self.redactor,
            request=request,
            op=op,
            meta=meta,
            validation=validation,
            route=route,
            auth_headers=self.auth.preview_headers(),
            confirm=confirm,
            dry_run=dry_run,
        )
        if gated is not None:
            return gated
        if self.config.restBaseUrl is None:
            return CallOutcome(error=registry.error("IR-1003"), meta=meta)
        return await self._execute(request, op, meta)

    async def _execute(
        self, request: PreparedRequest, op: Mapping[str, Any], meta: dict[str, Any]
    ) -> CallOutcome:
        started = time.monotonic()
        meta["requestId"] = request.request_id
        context = ErrorContext(operation_id=request.operation_id, session_established=True)
        try:
            response = await self._send_authenticated(request)
        except AuthError as exc:
            return CallOutcome(error=exc.error, meta=meta)
        except TransportFailure as exc:
            cause: BaseException = TimeoutError(str(exc)) if exc.timeout else exc
            return CallOutcome(error=self.mapper.from_transport_error(cause, context), meta=meta)
        finally:
            meta["durationMs"] = round((time.monotonic() - started) * 1000)
        meta["attempts"] = response.attempts
        meta["httpStatus"] = response.status
        if response.status == 401:
            error = self.mapper.from_rest(401, response.content, context, str(op["surface"]))
            if not request.idempotent and error is not None:
                error["hint"] = (
                    "The server rejected the credentials before running this write, and writes "
                    "are never replayed automatically. The session has been renewed if "
                    "possible; repeat the call."
                )
            return CallOutcome(error=error, meta=meta)
        if request.method == "HEAD" and response.status in {200, 404}:
            return CallOutcome(data={"exists": response.status == 200}, meta=meta)
        error = self.mapper.from_rest(
            response.status, response.content, context, str(op["surface"])
        )
        if error is not None:
            return CallOutcome(error=error, meta=meta)
        return CallOutcome(data=_decode_body(response), meta=meta)

    async def _send_authenticated(self, request: PreparedRequest) -> RawResponse:
        """Attach credentials and send; on 401 re-authenticate once (single-flight) and replay
        only idempotent requests."""
        auth_headers = await self.auth.headers()
        response = await send_with_retries(
            self.transport, request.with_headers(auth_headers), self.retry, self.sleep
        )
        if response.status != 401:
            return response
        refreshed = await self.auth.refresh_after_401(auth_headers.get("Authorization"))
        if not refreshed or not request.idempotent:
            return response
        logger.info("replaying %s after re-authentication", request.operation_id)
        auth_headers = await self.auth.headers()
        return await send_with_retries(
            self.transport, request.with_headers(auth_headers), self.retry, self.sleep
        )

    def _finish(self, outcome: CallOutcome) -> CallOutcome:
        outcome.data = self.redactor.value(outcome.data)
        outcome.error = self.redactor.value(outcome.error) if outcome.error else None
        outcome.meta = self.redactor.value(outcome.meta)
        return outcome
