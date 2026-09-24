"""The standard result envelope every tool returns (plan §5.4)."""

from __future__ import annotations

import json
from typing import Any

from mcp.types import CallToolResult, TextContent


def to_envelope(
    data: Any = None,
    error: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> CallToolResult:
    """Wrap a result in the envelope; ``isError`` mirrors ``ok``."""
    full_meta: dict[str, Any] = {"warnings": []}
    full_meta.update(meta or {})
    envelope = {"ok": error is None, "data": data, "error": error, "meta": full_meta}
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(envelope, indent=2))],
        structured_content=envelope,
        is_error=error is not None,
    )
