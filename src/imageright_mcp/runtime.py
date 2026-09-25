"""Process-wide state behind the client tools: session-scoped config overrides (``ir_configure``)
and one long-lived ``RestClient`` whose REST token and SOAP session survive across tool calls.

The effective config is always ``load_config(env)`` with the overrides applied on top, so the
explorer tools see an overridden ``irVersion`` too. Secrets never come from overrides.

Credential binding: env credentials belong to the endpoints configured in env or the config
file. If an override moves ``restBaseUrl`` or ``soapUrl`` to another origin, the secrets are
withheld for the whole session (warning IR-1001) so a tool call cannot redirect the password,
JWT or SAML token to a different host.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from imageright_mcp.client import RestClient
from imageright_mcp.client.soap import logoff_all
from imageright_mcp.config import (
    _SECRETS,
    ConfigError,
    EffectiveConfig,
    load_config,
    resolve_profile,
    strip_userinfo,
)
from imageright_mcp.errors import get_registry

logger = logging.getLogger(__name__)

# What ir_configure may change for the session.
RUNTIME_SETTINGS = frozenset(
    {
        "irVersion",
        "surfacePreference",
        "writeMode",
        "dryRun",
        "requireConfirm",
        "strictVersion",
        "requireVerifiedMappings",
        "restBaseUrl",
        "soapUrl",
        "soapConnection",
        "soapInactivityMinutes",
        "fileRoots",
        "authMode",
        "username",
        "jwtSubject",
        "jwtIssuer",
        "jwtAudience",
        "jwtTtlSeconds",
        "timeoutSeconds",
        "maxRetries",
    }
)
# Non-secret, but they run commands, read key files, weaken TLS, copy env values into request
# headers or pick where downloads are written: env or config file only.
ENV_ONLY_SETTINGS = frozenset(
    {
        "outputDir",
        "samlTokenCommand",
        "jwtPrivateKeyFile",
        "extraHeaders",
        "secretEnv",
        "caBundle",
        "verifyTls",
        "requestIdHeader",
    }
)
_SECRET_LIKE = re.compile(r"password|passwd|secret|token|jwt$|privatekey|apikey", re.IGNORECASE)
_WITHHELD: dict[str, Any] = {**{name: None for name in _SECRETS}, "extraHeaderValues": {}}
# A change to any of these needs a new HTTP client / auth session; the rest apply in place.
CONNECTION_FIELDS = frozenset(
    {
        "restBaseUrl",
        "soapUrl",
        "soapConnection",
        "soapInactivityMinutes",
        "authMode",
        "username",
        "jwtSubject",
        "jwtIssuer",
        "jwtAudience",
        "jwtTtlSeconds",
        "jwtPrivateKeyFile",
        "samlTokenCommand",
        "extraHeaders",
        "secretEnv",
        "timeoutSeconds",
        "caBundle",
        "verifyTls",
        "requestIdHeader",
        "outputDir",
        *_SECRETS,
        "extraHeaderValues",
    }
)


class ConfigureError(Exception):
    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(error["message"])
        self.error = error


def _origin(url: str | None) -> tuple[str, str, int | None] | None:
    if not url:
        return None
    parts = urlsplit(strip_userinfo(url) or "")
    return parts.scheme.lower(), (parts.hostname or "").lower(), parts.port


def check_settings(settings: Mapping[str, Any]) -> None:
    """Reject secrets (IR-3005, per the tool contract), env-only and unknown keys (IR-3006).
    Values are never echoed back: they may be the very secret being refused."""
    registry = get_registry()
    for key in settings:
        if key in _SECRETS or (_SECRET_LIKE.search(key) and key not in RUNTIME_SETTINGS):
            if key in ENV_ONLY_SETTINGS:
                continue
            raise ConfigureError(
                registry.error(
                    "IR-3005",
                    message=f"{key} is a secret and is never accepted as a tool argument; "
                    "the value was discarded.",
                    hint="Set credentials in the environment of the MCP server "
                    "(IMAGERIGHT_PASSWORD, IMAGERIGHT_JWT, IMAGERIGHT_JWT_PRIVATE_KEY, "
                    "IMAGERIGHT_SAML_TOKEN) and restart it.",
                )
            )
    for key in settings:
        if key in ENV_ONLY_SETTINGS:
            raise ConfigureError(
                registry.error(
                    "IR-3006",
                    message=f"{key} can only be set in the environment or the config file.",
                    hint="It can run commands, read key files, weaken TLS, copy environment "
                    "values into headers or choose where files are written, so it is not "
                    "changeable at runtime.",
                )
            )
        if key not in RUNTIME_SETTINGS:
            raise ConfigureError(
                registry.error(
                    "IR-3006",
                    message=f"Unknown setting {key!r}. Runtime settings: "
                    f"{', '.join(sorted(RUNTIME_SETTINGS))}.",
                )
            )


def apply_overrides(
    base: EffectiveConfig, overrides: Mapping[str, Any]
) -> tuple[EffectiveConfig, list[dict[str, str]]]:
    """``base`` with ``overrides`` applied; raises ``ConfigureError`` (IR-3006) on bad values."""
    if not overrides:
        return base, []
    warnings: list[dict[str, str]] = []
    values = base.model_dump()
    values.update(overrides)
    values["profile"] = resolve_profile(str(values["irVersion"]))
    values["sources"] = {**base.sources, **{k: "runtime" for k in overrides}}
    moved = [
        name
        for name in ("restBaseUrl", "soapUrl")
        if name in overrides and _origin(overrides[name]) != _origin(getattr(base, name))
    ]
    if moved and base.secret_values():
        values.update(_WITHHELD)
        warnings.append(
            get_registry().warning(
                "IR-1001",
                f"{' and '.join(moved)} now point(s) at a different host than the configured "
                "one, so credentials from the environment are withheld for this session. "
                "Change the endpoint in the environment instead, or ir_configure reset=true.",
            )
        )
    try:
        return EffectiveConfig(**values), warnings
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise ConfigureError(
            get_registry().error("IR-3006", message=f"Invalid setting value(s): {problems}.")
        ) from None


ClientFactory = Callable[[EffectiveConfig, RestClient | None], RestClient]


def _default_factory(config: EffectiveConfig, previous: RestClient | None) -> RestClient:
    # Keep the redactor so secrets and tokens seen earlier stay masked in logs.
    return RestClient(config, redactor=previous.redactor if previous else None)


class Runtime:
    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        *,
        client_factory: ClientFactory = _default_factory,
    ) -> None:
        self.env = env
        self.overrides: dict[str, Any] = {}
        self.client_factory = client_factory
        self._client: RestClient | None = None

    def config(self) -> EffectiveConfig:
        """Raises ``ConfigError`` for a broken env / file, ``ConfigureError`` for overrides."""
        config, _ = apply_overrides(load_config(self.env), self.overrides)
        return config

    def config_warnings(self) -> list[dict[str, str]]:
        _, warnings = apply_overrides(load_config(self.env), self.overrides)
        return warnings

    async def client(self) -> RestClient:
        """The shared client, rebuilt (old sessions logged off) when a connection setting
        changed and updated in place otherwise."""
        config = self.config()
        current = self._client
        if current is not None and current.config == config:
            return current
        if current is not None and not _connection_changed(current.config, config):
            current.reconfigure(config)
            return current
        if current is not None:
            await current.aclose()
        client = self.client_factory(config, current)
        if current is not None:
            client.adopt_ledger(current)
        self._client = client
        return client

    def peek_client(self) -> RestClient | None:
        return self._client

    def configure(
        self, settings: Mapping[str, Any], *, reset: bool = False
    ) -> tuple[EffectiveConfig, list[str], list[dict[str, str]]]:
        """Apply session overrides. Returns ``(config, changed keys, warnings)``."""
        check_settings(settings)
        try:
            base = load_config(self.env)
        except ConfigError as exc:
            raise ConfigureError(get_registry().error("IR-1001", message=str(exc))) from None
        merged = {} if reset else dict(self.overrides)
        merged.update(settings)
        before, _ = apply_overrides(base, self.overrides)
        config, warnings = apply_overrides(base, merged)
        self.overrides = merged
        changed = sorted(k for k in RUNTIME_SETTINGS if getattr(before, k) != getattr(config, k))
        if config.profile.approximated:
            warnings.append(get_registry().warning("IR-1005", config.profile.note))
        return config, changed, warnings

    async def aclose(self) -> None:
        """Shutdown: log off the SOAP session and drop tokens (plan §4.4 step 6)."""
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.aclose()
            except Exception as exc:  # shutdown is best effort
                logger.info("client close failed: %s", type(exc).__name__)
        await logoff_all()


def _connection_changed(old: EffectiveConfig, new: EffectiveConfig) -> bool:
    return any(getattr(old, f) != getattr(new, f) for f in CONNECTION_FIELDS)
