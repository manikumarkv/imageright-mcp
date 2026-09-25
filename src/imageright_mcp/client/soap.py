"""SOAP transport (plan §4.1, §4.4; M5): the ``irwebservice40.asmx`` pipeline.

``SoapClient`` runs the same steps as the REST client: validator -> write policy / dry-run gate
-> envelope builder -> ``SoapSession`` -> transport -> ErrorMapper (faults and result-level
failures) -> envelope. The HTTP POST itself goes through the shared ``Transport``.

``SoapSession`` owns the security token and a single queue:

* Every call (logins included) runs under one lock, so concurrency is 1 per session and calls
  complete in arrival order. Two in-flight calls holding the same token would race the rotation.
* ``UserLogin(username, password, connName)`` returns the first token. Each call sends the current
  token; the ``securityToken`` echoed in the response is stored as the current token before the
  lock is released, even when the result is a fault that still carries one. No echo keeps the
  old token. Tokens live in memory only, for the process lifetime.
* The server ends idle sessions (default 20 minutes). After that long without a call, the next
  call first asks ``IsLoggedIn``. A session-expired fault (or ``IsLoggedIn`` = false) triggers
  one re-login; a read is then replayed, a write returns IR-2007 and is never replayed.
* ``UserLogoff`` runs on shutdown, best effort (``logoff_all``).
"""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
import xml.etree.ElementTree as ET
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from imageright_mcp.catalog import Catalog
from imageright_mcp.client.auth import AuthError
from imageright_mcp.client.builder import BuildError
from imageright_mcp.client.models import (
    FileBase64,
    PreparedRequest,
    RawResponse,
    TransportFailure,
)
from imageright_mcp.client.pipeline import CallOutcome, policy_gate
from imageright_mcp.client.policy import PreviewLedger
from imageright_mcp.client.redact import Redactor
from imageright_mcp.client.soap_envelope import (
    EnvelopeBuilder,
    ParsedResponse,
    ResponseParser,
    SoapCall,
    SoapTable,
)
from imageright_mcp.client.transport import (
    BodySink,
    RetryPolicy,
    Sleep,
    Transport,
    send_with_retries,
)
from imageright_mcp.client.validator import Issue, Validator
from imageright_mcp.config import EffectiveConfig, strip_userinfo
from imageright_mcp.errors import ErrorContext, ErrorMapper, get_registry
from imageright_mcp.errors.mapper import MAX_RAW

logger = logging.getLogger(__name__)

LOGIN_OP = "soap.UserLogin"
LOGOFF_OP = "soap.UserLogoff"
IS_LOGGED_IN_OP = "soap.IsLoggedIn"
CONNECTIONS_OP = "soap.AvailableConnections"
# The session manager calls these itself; credentials are never tool arguments (plan §4.4).
SESSION_OPS = frozenset({LOGIN_OP, LOGOFF_OP})
SESSION_EXPIRED = "IR-2007"
WRITE_EXPIRED_HINT = (
    "The SOAP session expired during this write, so its outcome is unknown (state unknown; "
    "verify before retrying). The session has been renewed; writes are never replayed "
    "automatically."
)
LOGOFF_TIMEOUT = 5.0
# Rotated-out tokens stay registered with the Redactor for this many rotations.
REDACTED_TOKEN_WINDOW = 64

# Sessions that hold a token, for UserLogoff at shutdown.
_LIVE: weakref.WeakSet[SoapSession] = weakref.WeakSet()


async def logoff_all() -> int:
    """Best-effort ``UserLogoff`` for every live session; returns how many succeeded."""
    sessions = list(_LIVE)
    if not sessions:
        return 0
    results = await asyncio.gather(*(s.logoff() for s in sessions), return_exceptions=True)
    return sum(1 for r in results if r is True)


@dataclass
class Exchange:
    """What one SOAP call produced, after token handling and error mapping."""

    result: Any = None
    error: dict[str, Any] | None = None
    response: RawResponse | None = None
    shape: str | None = None
    replayed: bool = False
    relogged: bool = False


@dataclass(frozen=True)
class SoapSettings:
    url: str | None
    username: str | None
    password: str | None
    connection: str | None
    inactivity_seconds: float

    @classmethod
    def from_config(cls, config: EffectiveConfig) -> SoapSettings:
        return cls(
            url=strip_userinfo(config.soapUrl),
            username=config.username,
            password=config.password,
            connection=config.soapConnection,
            inactivity_seconds=config.soapInactivityMinutes * 60.0,
        )


class SoapSession:
    def __init__(
        self,
        settings: SoapSettings,
        *,
        transport: Transport,
        table: SoapTable,
        catalog_ops: Mapping[str, Mapping[str, Any]],
        redactor: Redactor,
        mapper: ErrorMapper,
        sink: BodySink,
        retry: RetryPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.table = table
        self.ops = catalog_ops
        self.redactor = redactor
        self.mapper = mapper
        self.sink = sink
        self.retry = retry or RetryPolicy()
        self.clock = clock
        self.sleep = sleep
        self.builder = EnvelopeBuilder(table)
        self.parser = ResponseParser(table, self._write_binary)
        self.registry = get_registry()
        self._queue = asyncio.Lock()
        self._token: str | None = None
        self._recent: deque[str] = deque()
        self._last_used: float | None = None
        self.connection = settings.connection
        self.logins = 0
        self.rotations = 0
        self.notes: list[str] = []
        redactor.add(settings.password)

    # ------------------------------------------------------------------ public API

    @property
    def active(self) -> bool:
        return self._token is not None

    async def run(self, call: SoapCall) -> Exchange:
        """Send one call through the queue: session setup, token rotation, expiry handling."""
        async with self._queue:
            relogged = await self._ensure_session(call)
            exchange = await self._exchange(call)
            if call.token_arg is not None and self._expired(exchange):
                logger.info("SOAP session expired during %s; logging in again", call.operation)
                await self._login()
                if call.idempotent:
                    exchange = await self._exchange(call)
                    exchange.replayed = True
                elif exchange.error is not None:
                    exchange.error = self.registry.error(
                        SESSION_EXPIRED,
                        hint=WRITE_EXPIRED_HINT,
                        native=exchange.error.get("native"),
                    )
                relogged = True
            exchange.relogged = relogged
            return exchange

    async def login(self) -> None:
        """Log in now (``ir_session`` refresh, ``ir_test_connection``); replaces any token."""
        async with self._queue:
            await self._login()

    async def available_connections(self) -> list[str]:
        """Connection names for ``UserLogin`` (``ir_test_connection`` lists them)."""
        async with self._queue:
            return await self._connections()

    async def logoff(self) -> bool:
        """``UserLogoff``, best effort: waits for the queue (at most ``LOGOFF_TIMEOUT`` seconds
        overall), never raises, drops the token."""
        try:
            async with asyncio.timeout(LOGOFF_TIMEOUT):
                return await self._logoff()
        except Exception as exc:  # shutdown must not fail because the server is gone
            logger.info("SOAP logoff skipped (%s)", type(exc).__name__)
            self._forget()
            return False

    def status(self) -> dict[str, Any]:
        return {
            "surface": "soap",
            "authenticated": self._token is not None,
            "connection": self.connection,
            "logins": self.logins,
            "tokenRotations": self.rotations,
            "idleTimeoutSeconds": self.settings.inactivity_seconds,
            "notes": list(self.notes),
        }

    # ------------------------------------------------------------------ session

    async def _ensure_session(self, call: SoapCall) -> bool:
        """Log in when there is no token; after the idle timeout, check IsLoggedIn first so a
        write is not the call that discovers the expiry. True when a login happened."""
        if call.token_arg is None:
            return False
        if self._token is None:
            await self._login()
            return True
        idle = self._last_used is not None and (
            self.clock() - self._last_used >= self.settings.inactivity_seconds
        )
        if not idle:
            return False
        probe = await self._exchange(self._call(IS_LOGGED_IN_OP, {}))
        if self._expired(probe):
            logger.info("SOAP session idle past its timeout; logging in again")
            await self._login()
            return True
        return False

    def _expired(self, exchange: Exchange) -> bool:
        # IR-2007 covers both a session-expired fault and IsLoggedIn = false (result failure).
        return exchange.error is not None and exchange.error["code"] == SESSION_EXPIRED

    async def _login(self) -> None:
        settings = self.settings
        if not settings.username or not settings.password:
            raise AuthError(
                self.registry.error(
                    "IR-1001",
                    message="SOAP needs a user name and a password.",
                    hint="Set IMAGERIGHT_USERNAME and IMAGERIGHT_PASSWORD. Credentials are "
                    "never accepted as tool arguments.",
                )
            )
        self._forget()
        connection = self.connection or await self._pick_connection()
        call = self._call(
            LOGIN_OP,
            {"username": settings.username, "password": settings.password, "connName": connection},
        )
        exchange = await self._exchange(call)
        error = exchange.error
        if error is not None:
            if error["code"] in {"IR-5003", SESSION_EXPIRED}:
                error = self.registry.error("IR-2001", native=error.get("native"))
            raise AuthError(self.redactor.value(error))
        token = exchange.result
        if not isinstance(token, str) or not token.strip():
            raise AuthError(
                self.registry.error("IR-2001", message="UserLogin returned no security token.")
            )
        self._store(token.strip())
        self.logins += 1
        _LIVE.add(self)
        logger.info("SOAP login succeeded (login #%d)", self.logins)

    async def _connections(self) -> list[str]:
        exchange = await self._exchange(self._call(CONNECTIONS_OP, {}))
        if exchange.error is not None:
            raise AuthError(exchange.error)
        return [str(n) for n in exchange.result or [] if n]

    async def _pick_connection(self) -> str:
        names = await self._connections()
        if len(names) == 1:
            self.connection = names[0]
            note = f"soapConnection is not set; using the only connection, {names[0]!r}."
            if note not in self.notes:
                self.notes.append(note)
            return names[0]
        raise AuthError(
            self.registry.error(
                "IR-1001",
                message="soapConnection is not set and the server offers "
                f"{len(names)} connections: {', '.join(names) or 'none'}.",
                hint="Set IMAGERIGHT_SOAP_CONNECTION to one of them (names are case-sensitive).",
                native={"surface": "soap", "availableConnections": names},
            )
        )

    async def _logoff(self) -> bool:
        async with self._queue:
            if self._token is None:
                return False
            try:
                exchange = await self._exchange(self._call(LOGOFF_OP, {}))
            finally:
                self._forget()
            return exchange.error is None

    # ------------------------------------------------------------------ one exchange

    def _call(self, op_id: str, params: Mapping[str, Any]) -> SoapCall:
        return self.builder.build(self.ops[op_id], params, self.settings.url)

    def _store(self, token: str) -> None:
        """Make ``token`` current; keep a bounded window of older ones registered as secrets."""
        self.redactor.add(token)
        if token != self._token:
            if self._token is not None:
                self.rotations += 1
            self._recent.append(token)
            while len(self._recent) > REDACTED_TOKEN_WINDOW:
                self.redactor.discard(self._recent.popleft())
        self._token = token

    def _forget(self) -> None:
        self._token = None
        _LIVE.discard(self)

    async def _exchange(self, call: SoapCall) -> Exchange:
        """POST one envelope with the current token and store the echoed token before anything
        else can run. Never raises for upstream failures; they become ``Exchange.error``."""
        context = ErrorContext(operation_id=call.operation_id, session_established=True)
        request = call.prepared(self._token)
        try:
            response = await send_with_retries(self.transport, request, self.retry, self.sleep)
        except TransportFailure as exc:
            cause: BaseException = TimeoutError(str(exc)) if exc.timeout else exc
            return Exchange(error=self.mapper.from_transport_error(cause, context))
        self._last_used = self.clock()
        try:
            parsed = self.parser.parse(call, response.content)
        except ET.ParseError as exc:
            return Exchange(error=self._unparsable(response, context, str(exc)), response=response)
        if parsed.token and call.token_arg is not None:
            # Rotation (plan §4.4): stored before the queue releases, fault or not.
            self._store(parsed.token)
        return self._classify(call, response, parsed, context)

    def _classify(
        self,
        call: SoapCall,
        response: RawResponse,
        parsed: ParsedResponse,
        context: ErrorContext,
    ) -> Exchange:
        if parsed.fault:
            error = self.mapper.from_soap_fault(response.content, context)
            return Exchange(error=error, response=response)
        if parsed.unexpected is not None:
            error = self.mapper.from_rest(response.status, b"", context, "soap")
            if error is None:
                error = self.registry.error(
                    "IR-9002",
                    message=f"The SOAP response had an {parsed.unexpected}.",
                    native={"surface": "soap", "httpStatus": response.status},
                )
            return Exchange(error=error, response=response)
        if not 200 <= response.status < 300:
            error = self.mapper.from_rest(response.status, b"", context, "soap")
            return Exchange(error=error, response=response)
        error = self.mapper.from_soap_result(call.operation_id, parsed.result, context)
        return Exchange(result=parsed.result, error=error, response=response, shape=parsed.shape)

    def _unparsable(self, response: RawResponse, context: ErrorContext, why: str) -> dict[str, Any]:
        # An IIS error page, a proxy login form, a 404 for a wrong soapUrl: judge by status.
        error = self.mapper.from_rest(response.status, b"", context, "soap")
        if error is not None:
            error["native"] = {**(error.get("native") or {}), "parseError": why}
            return error
        raw = response.content.decode("utf-8", "replace")[:MAX_RAW]
        return self.registry.error(
            "IR-9002",
            message="The SOAP response is not XML.",
            native={"surface": "soap", "httpStatus": response.status, "raw": raw},
        )

    def _write_binary(self, data: bytes, stem: str) -> dict[str, Any]:
        """Binary results become a file reference (plan §4.5), like REST binary bodies."""
        request = PreparedRequest(method="POST", url="", operation_id=f"soap.{stem}")

        async def one_chunk() -> AsyncIterator[bytes]:
            yield data

        # The parser is synchronous; the sink's async writer does no real awaiting.
        coro = self.sink.write(request, "application/octet-stream", one_chunk())
        try:
            coro.send(None)
        except StopIteration as done:
            ref = done.value
        else:  # pragma: no cover - BodySink.write never suspends
            coro.close()
            raise RuntimeError("BodySink.write suspended")
        return dict(ref.to_dict())


def _has_local_files(value: Any) -> bool:
    if isinstance(value, FileBase64):
        return True
    if isinstance(value, Mapping):
        return any(_has_local_files(v) for v in value.values())
    if isinstance(value, list | tuple):
        return any(_has_local_files(v) for v in value)
    return False


class SoapClient:
    """Pipeline steps 3-8 for SOAP operations; created and driven by ``RestClient.call``."""

    def __init__(
        self,
        config: EffectiveConfig,
        *,
        transport: Transport,
        catalog: Catalog,
        redactor: Redactor,
        mapper: ErrorMapper,
        validator: Validator,
        ledger: PreviewLedger,
        retry: RetryPolicy,
        sink: BodySink,
        clock: Callable[[], float] = time.time,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.config = config
        self.catalog = catalog
        self.redactor = redactor
        self.validator = validator
        self.ledger = ledger
        self.table = SoapTable(catalog.soap_table)
        self.builder = EnvelopeBuilder(self.table)
        self.url_had_credentials = strip_userinfo(config.soapUrl) != config.soapUrl
        self.session = SoapSession(
            SoapSettings.from_config(config),
            transport=transport,
            table=self.table,
            catalog_ops=catalog.ops,
            redactor=redactor,
            mapper=mapper,
            sink=sink,
            retry=retry,
            clock=clock,
            sleep=sleep,
        )

    async def aclose(self) -> None:
        await self.session.logoff()

    async def call(
        self,
        op: Mapping[str, Any],
        params: dict[str, Any],
        files: Mapping[str, str],
        meta: dict[str, Any],
        *,
        dry_run: bool | None,
        confirm: str | None,
        route: list[dict[str, Any]] | None = None,
        extra_issues: list[Issue] | None = None,
    ) -> CallOutcome:
        registry = get_registry()
        if route is None:
            route = [{"surface": "soap", "chosen": True, "reason": "explicit operationId"}]
        meta["route"] = route
        if str(op["id"]) in SESSION_OPS:
            error = registry.error(
                "IR-3006",
                message=f"{op['operation']} is run by the server's SOAP session manager and "
                "cannot be called directly.",
                hint="The session logs in with the configured credentials on the first SOAP "
                "call and logs off at shutdown.",
            )
            return CallOutcome(error=error, meta=meta)
        for name, value in params.items():
            if "password" in name.lower() and isinstance(value, str):
                self.redactor.add(value)  # e.g. ChangeUserPassword.newPassword
        profile = str(meta["profile"])
        validation = self.validator.validate(op, profile, params, files)
        validation.issues[:0] = extra_issues or []
        meta["warnings"].extend(validation.warnings)
        try:
            # Local files sent as base64 show as a placeholder in the preview (and its hash).
            call = self.builder.build(op, params, self.session.settings.url, inline_files=False)
        except BuildError as exc:
            return CallOutcome(error=exc.error, meta=meta)
        if self.url_had_credentials:
            meta["warnings"].append(
                registry.warning(
                    "IR-1001",
                    "soapUrl contains user:password@; it is ignored. Use the auth settings.",
                )
            )
        if self.config.soapUrl is None:
            meta["warnings"].append(
                registry.warning("IR-1003", "soapUrl is not set; the preview uses a placeholder.")
            )
        gated = policy_gate(
            config=self.config,
            ledger=self.ledger,
            redactor=self.redactor,
            request=call.preview(),
            op=op,
            meta=meta,
            validation=validation,
            route=route,
            auth_headers={},
            confirm=confirm,
            dry_run=dry_run,
        )
        if gated is not None:
            return gated
        if self.config.soapUrl is None:
            error = registry.error("IR-1003", message="soapUrl is not configured.")
            return CallOutcome(error=error, meta=meta)
        if _has_local_files(params):
            try:
                call = self.builder.build(op, params, self.session.settings.url)
            except OSError as exc:
                error = registry.error(
                    "IR-3006", message=f"Cannot read the image file: {type(exc).__name__}."
                )
                return CallOutcome(error=error, meta=meta)
        return await self._execute(call, meta)

    async def _execute(self, call: SoapCall, meta: dict[str, Any]) -> CallOutcome:
        started = time.monotonic()
        try:
            exchange = await self.session.run(call)
        except AuthError as exc:
            return CallOutcome(error=exc.error, meta=meta)
        finally:
            meta["durationMs"] = round((time.monotonic() - started) * 1000)
        if exchange.response is not None:
            meta["requestId"] = exchange.response.request_id
            meta["attempts"] = exchange.response.attempts
            meta["httpStatus"] = exchange.response.status
        if exchange.shape:
            meta["shape"] = exchange.shape
        if exchange.replayed:
            meta["replayedAfterRelogin"] = True
        if exchange.error is not None:
            return CallOutcome(error=exchange.error, meta=meta)
        return CallOutcome(data=exchange.result, meta=meta)


__all__ = [
    "Exchange",
    "SoapClient",
    "SoapSession",
    "SoapSettings",
    "logoff_all",
]
