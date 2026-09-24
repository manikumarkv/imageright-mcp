"""REST AuthManager (plan §4.4): one per surface; tokens live in memory only.

Modes, in priority order:

* ``password``: ``POST /api/authenticate`` -> ``Authorization: AccessToken <token>``;
  ``POST /api/validto`` gives the expiry; renewed ~60 s before it.
* ``jwt``: a static JWT from the environment, or a short-lived RS256 JWT self-signed with a
  configured private key -> ``Authorization: JWT <jwt>``.
* ``saml``: a base64 SAML token from the environment or ``samlTokenCommand`` ->
  ``Authorization: SecurityToken <token>``.

A 401 triggers one single-flight refresh: concurrent callers wait on the same login. Credentials
come only from configuration (environment), never from tool arguments, and are never logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jwt as pyjwt

from imageright_mcp.client.models import PreparedRequest, TransportFailure
from imageright_mcp.client.redact import Redactor
from imageright_mcp.client.transport import Transport
from imageright_mcp.config import REDACTED, AuthMode, EffectiveConfig
from imageright_mcp.errors import ErrorContext, ErrorMapper, get_registry

logger = logging.getLogger(__name__)

SCHEMES: dict[str, str] = {"password": "AccessToken", "jwt": "JWT", "saml": "SecurityToken"}
AUTHENTICATE_OP = "rest.v1.authentication.authenticate"
VALID_TO_OP = "rest.v1.authentication.validTo"
RENEW_MARGIN = 60.0
# Used when /api/validto cannot be read: assume a short life and renew early.
FALLBACK_LIFETIME = 15 * 60.0
COMMAND_TIMEOUT = 30.0

CommandRunner = Callable[[list[str]], Awaitable[str]]


class AuthError(Exception):
    """Credentials could not be obtained; ``error`` is an envelope error object."""

    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(error["message"])
        self.error = error


@dataclass(frozen=True)
class AuthSettings:
    mode: AuthMode
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    jwt: str | None = field(default=None, repr=False)
    jwt_private_key: str | None = field(default=None, repr=False)
    jwt_private_key_file: str | None = None
    jwt_subject: str | None = None
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    jwt_ttl: int = 300
    saml_token: str | None = field(default=None, repr=False)
    saml_command: list[str] | None = None
    extra_headers: dict[str, str] = field(default_factory=dict, repr=False)

    @classmethod
    def from_config(cls, config: EffectiveConfig) -> AuthSettings:
        command = config.samlTokenCommand
        return cls(
            mode=config.authMode,
            username=config.username,
            password=config.password,
            jwt=config.jwt,
            jwt_private_key=config.jwtPrivateKey,
            jwt_private_key_file=config.jwtPrivateKeyFile,
            jwt_subject=config.jwtSubject,
            jwt_issuer=config.jwtIssuer,
            jwt_audience=config.jwtAudience,
            jwt_ttl=config.jwtTtlSeconds,
            saml_token=config.samlToken,
            saml_command=shlex.split(command) if isinstance(command, str) else command,
            extra_headers=dict(config.extraHeaderValues),
        )


async def run_command(args: list[str]) -> str:
    """Run ``samlTokenCommand`` without a shell; stdout is the token. Output is never logged."""
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), COMMAND_TIMEOUT)
    except TimeoutError:
        proc.kill()
        raise RuntimeError("timed out") from None
    if proc.returncode != 0:
        raise RuntimeError(f"exit code {proc.returncode}")
    return stdout.decode("utf-8", "replace").strip()


def _parse_scalar(content: bytes) -> Any:
    text = content.decode("utf-8", "replace").strip()
    try:
        return json.loads(text)
    except ValueError:
        return text


def _parse_expiry(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


class AuthManager:
    """Credentials for the REST surface (v1 and v2 share one session)."""

    def __init__(
        self,
        settings: AuthSettings,
        *,
        transport: Transport,
        base_url: str | None,
        operations: Mapping[str, Mapping[str, Any]],
        redactor: Redactor,
        mapper: ErrorMapper | None = None,
        clock: Callable[[], float] = time.time,
        command_runner: CommandRunner = run_command,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.base_url = (base_url or "").rstrip("/")
        self.operations = operations
        self.redactor = redactor
        self.mapper = mapper or ErrorMapper(operations=operations)
        self.clock = clock
        self.command_runner = command_runner
        self.registry = get_registry()
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at: float | None = None
        self.logins = 0
        self.notes: list[str] = []
        for name, value in settings.extra_headers.items():
            redactor.add_header(name)
            redactor.add(value)
        for secret in (settings.password, settings.jwt, settings.jwt_private_key):
            redactor.add(secret)
        redactor.add(settings.saml_token)

    # ------------------------------------------------------------------ public API

    @property
    def scheme(self) -> str:
        return SCHEMES[self.settings.mode]

    def extra_headers(self) -> dict[str, str]:
        return dict(self.settings.extra_headers)

    def preview_headers(self) -> dict[str, str]:
        """What a dry-run shows: the scheme, never a value. Needs no credentials."""
        headers = {"Authorization": f"{self.scheme} {REDACTED}"}
        headers.update({name: REDACTED for name in self.settings.extra_headers})
        return headers

    async def headers(self) -> dict[str, str]:
        token = await self._current()
        return {"Authorization": f"{self.scheme} {token}", **self.settings.extra_headers}

    async def refresh_after_401(self, rejected_authorization: str | None) -> bool:
        """Single-flight re-authentication after a 401. True when fresh credentials exist.

        If another caller already replaced the rejected token, reuse theirs instead of logging
        in again, so N concurrent 401s cause one login.
        """
        async with self._lock:
            current = f"{self.scheme} {self._token}" if self._token else None
            if current is not None and current != rejected_authorization and self._valid():
                return True
            if not self._refreshable():
                self._token = None
                return False
            self._token = None
            await self._obtain()
            return True

    def status(self) -> dict[str, Any]:
        expires = (
            datetime.fromtimestamp(self._expires_at, UTC).isoformat()
            if self._expires_at is not None
            else None
        )
        return {
            "surface": "rest",
            "mode": self.settings.mode,
            "authenticated": self._token is not None and self._valid(),
            "expiresAt": expires,
            "logins": self.logins,
            "notes": list(self.notes),
        }

    def logout(self) -> None:
        self._token = None
        self._expires_at = None

    # ------------------------------------------------------------------ internals

    def _valid(self) -> bool:
        if self._token is None:
            return False
        if self._expires_at is None:
            return True
        margin = RENEW_MARGIN
        if self.settings.mode == "jwt" and not self.settings.jwt:
            margin = min(RENEW_MARGIN, self.settings.jwt_ttl / 2)
        return self.clock() < self._expires_at - margin

    def _refreshable(self) -> bool:
        mode = self.settings.mode
        if mode == "password":
            return True
        if mode == "jwt":
            return not self.settings.jwt  # a static JWT cannot be renewed
        return bool(self.settings.saml_command)

    async def _current(self) -> str:
        if self._valid():
            assert self._token is not None
            return self._token
        async with self._lock:
            if not self._valid():
                await self._obtain()
            assert self._token is not None
            return self._token

    async def _obtain(self) -> None:
        mode = self.settings.mode
        expires: float | None
        if mode == "password":
            token, expires = await self._password_login()
        elif mode == "jwt":
            token, expires = self._jwt_token()
        else:
            token, expires = await self._saml_token()
        self.redactor.add(token)
        self._token = token
        self._expires_at = expires
        self.logins += 1
        logger.info("REST %s authentication succeeded (login #%d)", mode, self.logins)

    def _config_error(self, message: str, hint: str | None = None) -> AuthError:
        return AuthError(self.registry.error("IR-1001", message=message, hint=hint))

    def _url(self, op_id: str) -> str:
        if not self.base_url:
            raise AuthError(self.registry.error("IR-1003"))
        return self.base_url + str(self.operations[op_id]["path"])

    # password ------------------------------------------------------------------

    async def _password_login(self) -> tuple[str, float]:
        settings = self.settings
        if not settings.username or not settings.password:
            raise self._config_error(
                "Password authentication needs a user name and a password.",
                "Set IMAGERIGHT_USERNAME and IMAGERIGHT_PASSWORD (or name the password variable "
                "in secretEnv). Credentials are never accepted as tool arguments.",
            )
        request = PreparedRequest(
            method="POST",
            url=self._url(AUTHENTICATE_OP),
            operation_id=AUTHENTICATE_OP,
            headers=self.extra_headers(),
            json={"UserName": settings.username, "Password": settings.password},
            expects_scalar=True,
        )
        response = await self._send(request)
        if not 200 <= response.status < 300:
            error = self.mapper.from_rest(
                response.status,
                response.content,
                ErrorContext(operation_id=AUTHENTICATE_OP, object_kind="user"),
            )
            if error is None or error["code"] in {"IR-2004", "IR-2005"}:
                error = self.registry.error("IR-2001", native=(error or {}).get("native"))
            raise AuthError(self.redactor.value(error))
        body = _parse_scalar(response.content)
        if isinstance(body, Mapping):
            body = next((v for k, v in body.items() if "token" in str(k).lower()), None)
        if not isinstance(body, str) or not body:
            raise AuthError(
                self.registry.error(
                    "IR-9002",
                    message="POST /api/authenticate returned no token.",
                    native={"surface": "rest-v1", "httpStatus": response.status},
                )
            )
        self.redactor.add(body)
        return body, await self._valid_to(body)

    async def _valid_to(self, token: str) -> float:
        request = PreparedRequest(
            method="POST",
            url=self._url(VALID_TO_OP),
            operation_id=VALID_TO_OP,
            headers={"Authorization": f"AccessToken {token}", **self.extra_headers()},
            text=token,
            expects_scalar=True,
        )
        try:
            response = await self._send(request)
        except AuthError:
            response = None
        expires = (
            _parse_expiry(_parse_scalar(response.content))
            if response is not None and 200 <= response.status < 300
            else None
        )
        if expires is None:
            note = "POST /api/validto gave no expiry; renewing the token every 15 minutes."
            if note not in self.notes:
                self.notes.append(note)
            logger.warning(note)
            return self.clock() + FALLBACK_LIFETIME
        return expires

    async def _send(self, request: PreparedRequest) -> Any:
        try:
            return await self.transport.send(request)
        except TransportFailure as exc:
            raise AuthError(
                self.redactor.value(
                    self.mapper.from_transport_error(
                        TimeoutError(str(exc)) if exc.timeout else exc,
                        ErrorContext(operation_id=request.operation_id),
                    )
                )
            ) from None

    # jwt -----------------------------------------------------------------------

    def _jwt_token(self) -> tuple[str, float | None]:
        settings = self.settings
        now = self.clock()
        if settings.jwt:
            try:
                claims = pyjwt.decode(settings.jwt, options={"verify_signature": False})
            except pyjwt.PyJWTError:
                raise self._config_error(
                    "IMAGERIGHT_JWT does not hold a readable JWT.",
                    "Paste the compact JWT (three base64url parts separated by dots).",
                ) from None
            exp = claims.get("exp")
            if isinstance(exp, int | float) and exp <= now:
                raise AuthError(
                    self.registry.error(
                        "IR-2004",
                        message="The static JWT from the environment has expired.",
                        hint="Issue a new JWT and restart the server, or configure "
                        "IMAGERIGHT_JWT_PRIVATE_KEY so the server can sign short-lived tokens.",
                    )
                )
            return settings.jwt, float(exp) if isinstance(exp, int | float) else None
        key = settings.jwt_private_key
        if not key and settings.jwt_private_key_file:
            try:
                key = Path(settings.jwt_private_key_file).expanduser().read_text("utf-8")
            except OSError:
                raise self._config_error("The JWT private key file cannot be read.") from None
        subject = settings.jwt_subject or settings.username
        if not key or not subject or not settings.jwt_issuer or not settings.jwt_audience:
            raise self._config_error(
                "JWT authentication needs a static IMAGERIGHT_JWT, or a private key plus "
                "subject, issuer and audience.",
                "Set IMAGERIGHT_JWT, or IMAGERIGHT_JWT_PRIVATE_KEY(_FILE), "
                "IMAGERIGHT_JWT_SUBJECT (or USERNAME), IMAGERIGHT_JWT_ISSUER and "
                "IMAGERIGHT_JWT_AUDIENCE.",
            )
        issued = int(now)
        expires = issued + settings.jwt_ttl
        claims = {
            "sub": subject,
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "iat": issued,
            "nbf": issued - 30,  # tolerate small clock skew on the server
            "exp": expires,
        }
        try:
            token = pyjwt.encode(claims, key, algorithm="RS256")
        except (ValueError, TypeError, pyjwt.PyJWTError):
            raise self._config_error(
                "The JWT private key is not a usable RSA private key (PEM).",
            ) from None
        return token, float(expires)

    # saml ----------------------------------------------------------------------

    async def _saml_token(self) -> tuple[str, float | None]:
        settings = self.settings
        if settings.saml_command:
            try:
                token = await self.command_runner(list(settings.saml_command))
            except (OSError, RuntimeError) as exc:
                raise self._config_error(
                    f"samlTokenCommand failed ({type(exc).__name__}: {exc}).",
                    "Run the command by hand to check it prints a base64 SAML token.",
                ) from None
            if not token:
                raise self._config_error("samlTokenCommand printed nothing.")
            return token, None
        if settings.saml_token:
            return settings.saml_token, None
        raise self._config_error(
            "SAML authentication needs IMAGERIGHT_SAML_TOKEN or samlTokenCommand.",
        )
