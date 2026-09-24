"""The standard result envelope (plan §5.4). Every tool handler returns through ``to_envelope``.

Errors and warnings are checked against the IR registry here, so a handler can never emit a code
that ``ir_explain_error`` does not know or a name that disagrees with the registry.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from mcp.types import CallToolResult, TextContent

from imageright_mcp.errors import get_registry

logger = logging.getLogger(__name__)

ERROR_KEYS = ("code", "name", "category", "message", "retryable", "hint", "native")


def _normalize_error(error: Mapping[str, Any]) -> dict[str, Any]:
    registry = get_registry()
    code = error.get("code")
    entry = registry.entries.get(str(code))
    if entry is None or error.get("name", entry["name"]) != entry["name"]:
        logger.error("tool produced an error outside the registry: %r", dict(error))
        return registry.error(
            "IR-9001",
            message=f"A tool produced error {code!r} {error.get('name')!r}, which is not in "
            "the registry.",
            native={"surface": None, "original": dict(error)},
        )
    full = registry.error(
        entry["code"],
        message=error.get("message"),
        hint=error.get("hint"),
        native=error.get("native"),
    )
    full.update({k: v for k, v in error.items() if k not in ERROR_KEYS})
    return full


def _normalize_warning(item: Mapping[str, Any]) -> dict[str, Any]:
    registry = get_registry()
    code = str(item.get("code"))
    if code not in registry.entries:
        logger.error("tool produced a warning outside the registry: %r", dict(item))
        return registry.warning("IR-9001", f"Unregistered warning {code!r}: {item.get('message')}")
    return {**item, **registry.warning(code, item.get("message"))}


def internal_error(exc: BaseException) -> dict[str, Any]:
    """IR-9001 for an unexpected exception in a handler (a bug in this server)."""
    logger.exception("unexpected error in tool handler", exc_info=exc)
    return get_registry().error(
        "IR-9001", native={"surface": None, "exception": type(exc).__name__, "message": str(exc)}
    )


def to_envelope(
    data: Any = None,
    error: Mapping[str, Any] | None = None,
    meta: Mapping[str, Any] | None = None,
) -> CallToolResult:
    """Wrap a result in the envelope; ``isError`` mirrors ``ok``."""
    full_meta: dict[str, Any] = {"warnings": []}
    full_meta.update(meta or {})
    full_meta["warnings"] = [_normalize_warning(w) for w in full_meta["warnings"]]
    normalized = _normalize_error(error) if error is not None else None
    envelope = {
        "ok": normalized is None,
        "data": data if normalized is None else None,
        "error": normalized,
        "meta": full_meta,
    }
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(envelope, indent=2))],
        structured_content=envelope,
        is_error=normalized is not None,
    )
