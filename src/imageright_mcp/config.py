"""Effective configuration: environment > config file > defaults.

Credentials are only ever read from the environment and are never exposed unredacted. A config
file may name *which* environment variable holds a secret (``secretEnv``, ``extraHeaders``) but
never the secret itself.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field

Source = Literal["env", "file", "default"]
WriteMode = Literal["deny", "dry-run", "allow"]
AuthMode = Literal["password", "jwt", "saml"]
Surface = Literal["rest-v2", "rest-v1", "soap"]

ENV_PREFIX = "IMAGERIGHT_"
CONFIG_FILE_ENV = "IMAGERIGHT_CONFIG_FILE"
REDACTED = "***"

# Non-secret settings: field name -> env var suffix. The config file uses the field names as keys.
_SETTINGS: dict[str, str] = {
    "irVersion": "VERSION",
    "restBaseUrl": "REST_BASE_URL",
    "soapUrl": "SOAP_URL",
    "soapConnection": "SOAP_CONNECTION",
    "soapInactivityMinutes": "SOAP_INACTIVITY_MINUTES",
    "authMode": "AUTH_MODE",
    "surfacePreference": "SURFACE_PREFERENCE",
    "writeMode": "WRITE_MODE",
    "dryRun": "DRY_RUN",
    "username": "USERNAME",
    "requireConfirm": "REQUIRE_CONFIRM",
    "jwtSubject": "JWT_SUBJECT",
    "jwtIssuer": "JWT_ISSUER",
    "jwtAudience": "JWT_AUDIENCE",
    "jwtTtlSeconds": "JWT_TTL_SECONDS",
    "jwtPrivateKeyFile": "JWT_PRIVATE_KEY_FILE",
    "samlTokenCommand": "SAML_TOKEN_COMMAND",
    "extraHeaders": "EXTRA_HEADERS",
    "secretEnv": "SECRET_ENV",
    "timeoutSeconds": "TIMEOUT_SECONDS",
    "caBundle": "CA_BUNDLE",
    "verifyTls": "VERIFY_TLS",
    "requestIdHeader": "REQUEST_ID_HEADER",
    "maxRetries": "MAX_RETRIES",
    "outputDir": "OUTPUT_DIR",
    "fileRoots": "FILE_ROOTS",
}

# Secrets are env-only; a config file may not contain them. ``secretEnv`` can rename the
# variable a secret is read from (e.g. ``{"password": "CORP_IR_PASSWORD"}``).
_SECRETS: dict[str, str] = {
    "password": "PASSWORD",
    "jwt": "JWT",
    "jwtPrivateKey": "JWT_PRIVATE_KEY",
    "samlToken": "SAML_TOKEN",
}
_BOOLS = {"dryRun", "requireConfirm", "verifyTls"}
_INTS = {"jwtTtlSeconds", "maxRetries"}
_FLOATS = {"timeoutSeconds", "soapInactivityMinutes"}
_MAPPINGS = {"extraHeaders", "secretEnv"}

_DEFAULTS: dict[str, Any] = {
    "irVersion": "24.x",
    "restBaseUrl": None,
    "soapUrl": None,
    "soapConnection": None,
    "soapInactivityMinutes": 20.0,
    "authMode": "password",
    "surfacePreference": ["rest-v2", "rest-v1", "soap"],
    "writeMode": "dry-run",
    "dryRun": False,
    "username": None,
    "requireConfirm": True,
    "jwtSubject": None,
    "jwtIssuer": None,
    "jwtAudience": None,
    "jwtTtlSeconds": 300,
    "jwtPrivateKeyFile": None,
    "samlTokenCommand": None,
    "extraHeaders": {},
    "secretEnv": {},
    "timeoutSeconds": 30.0,
    "caBundle": None,
    "verifyTls": True,
    "requestIdHeader": "X-Request-Id",
    "maxRetries": 2,
    "outputDir": None,
    "fileRoots": [],
}

_KNOWN_PROFILES: dict[tuple[int, int], str] = {(25, 1): "25.1", (24, 2): "24.2", (7, 2): "7.2"}


class ConfigError(ValueError):
    """Raised when a configuration source is unreadable or holds an invalid value."""


class ProfileResolution(BaseModel):
    profile: str | None
    approximated: bool
    note: str


class EffectiveConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    irVersion: str
    profile: ProfileResolution
    restBaseUrl: str | None
    soapUrl: str | None
    # UserLogin connection name (case-sensitive); unset means "ask AvailableConnections".
    soapConnection: str | None
    # The server ends an idle SOAP session after this long; the next call checks IsLoggedIn.
    soapInactivityMinutes: float = Field(gt=0)
    authMode: AuthMode
    surfacePreference: list[Surface]
    writeMode: WriteMode
    dryRun: bool
    username: str | None
    # A destructive call needs ``confirm: <previewId>`` from a prior dry-run (plan §7.1).
    requireConfirm: bool
    jwtSubject: str | None
    jwtIssuer: str | None
    jwtAudience: str | None
    jwtTtlSeconds: int = Field(gt=0, le=3600)
    jwtPrivateKeyFile: str | None
    samlTokenCommand: str | list[str] | None
    # Header name -> name of the environment variable holding its value (tenant ids, API keys).
    extraHeaders: dict[str, str]
    secretEnv: dict[str, str]
    timeoutSeconds: float = Field(gt=0)
    caBundle: str | None
    verifyTls: bool
    requestIdHeader: str
    maxRetries: int = Field(ge=0, le=5)
    outputDir: str | None
    fileRoots: list[str]
    password: str | None = Field(default=None, repr=False)
    jwt: str | None = Field(default=None, repr=False)
    jwtPrivateKey: str | None = Field(default=None, repr=False)
    samlToken: str | None = Field(default=None, repr=False)
    extraHeaderValues: dict[str, str] = Field(default_factory=dict, repr=False)
    configFile: str | None
    sources: dict[str, Source]

    def __repr__(self) -> str:
        return f"EffectiveConfig({self.redacted()!r})"

    __str__ = __repr__

    def redacted(self) -> dict[str, Any]:
        """Return a JSON-safe view with every secret replaced by a marker."""
        data = self.model_dump(mode="json", exclude={*_SECRETS, "extraHeaderValues"})
        for field in _SECRETS:
            data[field] = REDACTED if getattr(self, field) else None
        data["extraHeaders"] = {
            name: {"env": var, "set": name in self.extraHeaderValues}
            for name, var in self.extraHeaders.items()
        }
        data["restBaseUrl"] = redact_url(self.restBaseUrl)
        data["soapUrl"] = redact_url(self.soapUrl)
        return data

    def secret_values(self) -> list[str]:
        """Every secret this config holds, for redaction of previews, errors and logs."""
        values = [getattr(self, field) for field in _SECRETS]
        values.extend(self.extraHeaderValues.values())
        for url in (self.restBaseUrl, self.soapUrl):
            if url and urlsplit(url).password:
                values.append(urlsplit(url).password)
        return [v for v in values if v]


def redact_url(url: str | None) -> str | None:
    """Strip any userinfo password embedded in a URL (``https://user:pw@host``)."""
    if not url:
        return url
    parts = urlsplit(url)
    if parts.password is None:
        return url
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    netloc = f"{parts.username}:{REDACTED}@{host}"
    return urlunsplit(parts._replace(netloc=netloc))


def strip_userinfo(url: str | None) -> str | None:
    """Drop ``user:password@`` from a URL. Credentials in a URL are not an auth mode, and HTTP
    libraries log request URLs, so they must never reach the transport."""
    if not url:
        return url
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))


def resolve_profile(ir_version: str) -> ProfileResolution:
    """Map a configured IR version to a catalog profile (plan §4.2)."""
    unsupported = ProfileResolution(profile=None, approximated=False, note="unsupported version")
    match = re.fullmatch(r"(\d+)\.(x|\d+)(?:\.\d+)*", ir_version.strip().lower())
    if match is None:
        return unsupported
    major = int(match.group(1))
    same_major = sorted(k for k in _KNOWN_PROFILES if k[0] == major)
    if not same_major:
        return unsupported
    if match.group(2) == "x":
        profile = _KNOWN_PROFILES[same_major[-1]]
        return ProfileResolution(
            profile=profile, approximated=False, note=f"{major}.x -> {profile}"
        )
    key = (major, int(match.group(2)))
    if key in _KNOWN_PROFILES:
        profile = _KNOWN_PROFILES[key]
        return ProfileResolution(profile=profile, approximated=False, note=f"exact {profile}")
    if major == 7:
        return unsupported
    # Other 24.n / 25.n: nearest lower known profile, else the closest one in the same major.
    lower = [k for k in _KNOWN_PROFILES if k[0] >= 24 and k <= key]
    profile = _KNOWN_PROFILES[max(lower) if lower else same_major[0]]
    return ProfileResolution(
        profile=profile, approximated=True, note=f"approximated to nearest known {profile}"
    )


def _parse_env_value(field: str, raw: str) -> Any:
    name = ENV_PREFIX + _SETTINGS[field]
    if field == "surfacePreference":
        return [s.strip() for s in raw.split(",") if s.strip()]
    if field == "fileRoots":
        return [s.strip() for s in raw.split(os.pathsep) if s.strip()]
    if field in _BOOLS:
        lowered = raw.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise ConfigError(f"{name} must be a boolean, got {raw!r}")
    if field in _MAPPINGS:
        # "Header-Name=ENV_VAR,Other=ENV_VAR2"
        pairs = [item.partition("=") for item in raw.split(",") if item.strip()]
        if any(not sep or not key.strip() or not value.strip() for key, sep, value in pairs):
            raise ConfigError(f"{name} must look like Name=ENV_VAR[,Name=ENV_VAR]")
        return {key.strip(): value.strip() for key, _, value in pairs}
    if field in _INTS | _FLOATS:
        try:
            return int(raw) if field in _INTS else float(raw)
        except ValueError:
            raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    return raw


def _read_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config file {path} must contain a JSON object")
    secret_keys = sorted(set(data) & set(_SECRETS))
    if secret_keys:
        raise ConfigError(
            f"config file {path} must not contain secrets ({', '.join(secret_keys)}); "
            f"set them via {ENV_PREFIX}* environment variables"
        )
    unknown = sorted(set(data) - set(_SETTINGS))
    if unknown:
        raise ConfigError(f"config file {path} has unknown keys: {', '.join(unknown)}")
    return data


def load_config(env: Mapping[str, str] | None = None) -> EffectiveConfig:
    """Build the effective config from environment, optional JSON file, and defaults."""
    environ = os.environ if env is None else env
    file_path_raw = environ.get(CONFIG_FILE_ENV)
    file_path = Path(file_path_raw).expanduser() if file_path_raw else None
    file_values = _read_file(file_path) if file_path else {}

    values: dict[str, Any] = {}
    sources: dict[str, Source] = {}
    for field, suffix in _SETTINGS.items():
        env_raw = environ.get(ENV_PREFIX + suffix)
        if env_raw is not None and env_raw != "":
            values[field] = _parse_env_value(field, env_raw)
            sources[field] = "env"
        elif field in file_values:
            values[field] = file_values[field]
            sources[field] = "file"
        else:
            values[field] = _DEFAULTS[field]
            sources[field] = "default"

    secret_env = values["secretEnv"]
    if not isinstance(secret_env, dict) or set(secret_env) - set(_SECRETS):
        raise ConfigError(f"secretEnv may only name these secrets: {', '.join(_SECRETS)}")
    for field, suffix in _SECRETS.items():
        secret = environ.get(str(secret_env.get(field, ENV_PREFIX + suffix))) or None
        values[field] = secret
        sources[field] = "env" if secret else "default"

    headers = values["extraHeaders"]
    if not isinstance(headers, dict):
        raise ConfigError("extraHeaders must map header names to environment variable names")
    values["extraHeaderValues"] = {
        name: environ[str(var)] for name, var in headers.items() if environ.get(str(var))
    }

    try:
        return EffectiveConfig(
            **values,
            profile=resolve_profile(str(values["irVersion"])),
            configFile=str(file_path) if file_path else None,
            sources=sources,
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
