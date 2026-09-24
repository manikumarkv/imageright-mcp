"""REST pipeline, IrClient steps 3-7 (plan §4): validator -> write policy / dry-run gate ->
request builder -> AuthManager -> Transport.send, plus ErrorMapper on failure.

Routing (step 2) and normalizers (step 8) arrive in M6; here an explicit REST ``operationId`` is
executed as-is and the body is returned parsed (JSON / text) or as a file reference (binary).
Everything that leaves this module passes through the ``Redactor``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult

from imageright_mcp.catalog import Catalog, get_catalog
from imageright_mcp.client.auth import AuthError, AuthManager, AuthSettings, CommandRunner
from imageright_mcp.client.auth import run_command as default_runner
from imageright_mcp.client.builder import BuildError, RequestBuilder
from imageright_mcp.client.models import PreparedRequest, RawResponse, TransportFailure
from imageright_mcp.client.policy import PreviewLedger, build_preview, decide, preview_id
from imageright_mcp.client.redact import RedactingFilter, Redactor
from imageright_mcp.client.transport import (
    BodySink,
    RestTransport,
    RetryPolicy,
    Sleep,
    Transport,
    send_with_retries,
)
from imageright_mcp.client.validator import Validator
from imageright_mcp.config import EffectiveConfig, strip_userinfo
from imageright_mcp.envelope import to_envelope
from imageright_mcp.errors import ErrorContext, ErrorMapper, get_registry

logger = logging.getLogger(__name__)
PACKAGE_LOGGER = "imageright_mcp"


@dataclass
class CallOutcome:
    data: Any = None
    error: dict[str, Any] | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None

    def envelope(self) -> CallToolResult:
        return to_envelope(data=self.data, error=self.error, meta=self.meta)


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
        self.transport: Transport = transport or RestTransport(
            timeout=config.timeoutSeconds,
            verify=config.caBundle or config.verifyTls,
            request_id_header=config.requestIdHeader,
            sink=BodySink(Path(config.outputDir).expanduser() if config.outputDir else None),
        )
        self.mapper = ErrorMapper(operations=self.catalog.ops)
        self.validator = Validator(self.catalog.schemas)
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
        self._install_log_filter()

    def _install_log_filter(self) -> None:
        package_logger = logging.getLogger(PACKAGE_LOGGER)
        if not any(isinstance(f, RedactingFilter) for f in package_logger.filters):
            package_logger.addFilter(RedactingFilter(self.redactor))
        else:
            for f in package_logger.filters:
                if isinstance(f, RedactingFilter):
                    f.redactor = self.redactor

    async def aclose(self) -> None:
        await self.transport.aclose()

    # ------------------------------------------------------------------ pipeline

    async def call(
        self,
        operation_id: str,
        params: Mapping[str, Any] | None = None,
        files: Mapping[str, str] | None = None,
        *,
        dry_run: bool | None = None,
        confirm: str | None = None,
        capability_id: str | None = None,
    ) -> CallOutcome:
        params = dict(params or {})
        files = dict(files or {})
        registry = get_registry()
        profile = self.config.profile.profile
        meta: dict[str, Any] = {
            "irVersion": self.config.irVersion,
            "profile": profile,
            "operationId": operation_id,
            "capabilityId": capability_id,
            "dryRun": False,
            "warnings": [],
        }
        if profile is None:
            return self._finish(CallOutcome(error=registry.error("IR-1002"), meta=meta))
        op_id = self.catalog.find_operation_id(operation_id)
        if op_id is None:
            return self._finish(
                CallOutcome(
                    error=self.catalog.unknown_operation(operation_id).to_error(), meta=meta
                )
            )
        op = self.catalog.ops[op_id]
        meta["operationId"] = op_id
        meta["capabilityId"] = capability_id or op.get("capability")
        meta["surface"] = op["surface"]
        if op["surface"] == "soap":
            error = registry.error(
                "IR-3004", message="SOAP operations are not callable yet (SOAP transport: M5)."
            )
            return self._finish(CallOutcome(error=error, meta=meta))
        route = [{"surface": op["surface"], "chosen": True, "reason": "explicit operationId"}]
        meta["route"] = route

        validation = self.validator.validate(op, profile, params, files)
        meta["warnings"].extend(validation.warnings)
        try:
            request = self.builder.build(op, params, files)
        except BuildError as exc:
            return self._finish(CallOutcome(error=exc.error, meta=meta))
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
        pid = preview_id(request)
        decision = decide(
            safety=str(op["safety"]),
            write_mode=self.config.writeMode,
            config_dry_run=self.config.dryRun,
            dry_run=dry_run,
            require_confirm=self.config.requireConfirm,
            confirm=confirm,
            current_preview_id=pid,
            ledger=self.ledger,
        )
        meta["warnings"].extend(decision.warnings)

        def preview() -> dict[str, Any]:
            self.ledger.issue(pid)
            return build_preview(
                request=request,
                op=op,
                capability_id=meta["capabilityId"],
                auth_headers=self.auth.preview_headers(),
                validation=validation.to_dict(),
                route=route,
                warnings=list(meta["warnings"]),
                redactor=self.redactor,
            )

        if not validation.ok:
            first = validation.issues[0]
            error = registry.error(first.code, message=first.message)
            error["issues"] = [i.to_dict() for i in validation.issues]
            error["preview"] = preview()
            meta["dryRun"] = decision.action != "execute"
            return self._finish(CallOutcome(error=error, meta=meta))
        if decision.action == "block":
            assert decision.error is not None
            error = dict(decision.error)
            error["preview"] = preview()
            meta["dryRun"] = True
            return self._finish(CallOutcome(error=error, meta=meta))
        if decision.action == "preview":
            meta["dryRun"] = True
            meta["confirmRequired"] = decision.confirm_required
            return self._finish(CallOutcome(data={"preview": preview()}, meta=meta))
        if self.config.restBaseUrl is None:
            return self._finish(CallOutcome(error=registry.error("IR-1003"), meta=meta))
        return self._finish(await self._execute(request, op, meta))

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
