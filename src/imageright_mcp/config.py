"""Effective configuration: environment > config file > defaults.

Credentials are only ever read from the environment and are never exposed unredacted.
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
    "authMode": "AUTH_MODE",
    "surfacePreference": "SURFACE_PREFERENCE",
    "writeMode": "WRITE_MODE",
    "dryRun": "DRY_RUN",
    "username": "USERNAME",
}

# Secrets are env-only; a config file may not contain them.
_SECRETS: dict[str, str] = {
    "password": "PASSWORD",
}

_DEFAULTS: dict[str, Any] = {
    "irVersion": "24.x",
    "restBaseUrl": None,
    "soapUrl": None,
    "authMode": "password",
    "surfacePreference": ["rest-v2", "rest-v1", "soap"],
    "writeMode": "dry-run",
    "dryRun": False,
    "username": None,
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
    authMode: AuthMode
    surfacePreference: list[Surface]
    writeMode: WriteMode
    dryRun: bool
    username: str | None
    password: str | None = Field(default=None, repr=False)
    configFile: str | None
    sources: dict[str, Source]

    def __repr__(self) -> str:
        return f"EffectiveConfig({self.redacted()!r})"

    __str__ = __repr__

    def redacted(self) -> dict[str, Any]:
        """Return a JSON-safe view with every secret replaced by a marker."""
        data = self.model_dump(mode="json", exclude={"password"})
        data["password"] = REDACTED if self.password else None
        data["restBaseUrl"] = redact_url(self.restBaseUrl)
        data["soapUrl"] = redact_url(self.soapUrl)
        return data


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
    if field == "surfacePreference":
        return [s.strip() for s in raw.split(",") if s.strip()]
    if field == "dryRun":
        lowered = raw.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise ConfigError(f"{ENV_PREFIX}DRY_RUN must be a boolean, got {raw!r}")
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

    for field, suffix in _SECRETS.items():
        secret = environ.get(ENV_PREFIX + suffix) or None
        values[field] = secret
        sources[field] = "env" if secret else "default"

    try:
        return EffectiveConfig(
            **values,
            profile=resolve_profile(str(values["irVersion"])),
            configFile=str(file_path) if file_path else None,
            sources=sources,
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
