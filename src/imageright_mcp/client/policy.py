"""Write policy and dry-run gate (plan §7): decide execute / preview / block, and build previews.

* ``writeMode`` deny | dry-run | allow (default dry-run). ``write``/``destructive`` operations
  are previewed under dry-run and blocked (IR-3008) under deny.
* Per-call ``dryRun``: true always previews; false only takes effect under allow.
* ``requireConfirm``: a destructive call under allow needs ``confirm`` = the previewId of a prior
  dry-run of the identical request; anything else is IR-3009.

A preview needs no network, no auth and no credentials.
"""

from __future__ import annotations

import hashlib
import json
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from imageright_mcp.client.models import NO_BODY, FilePart, JsonPart, PreparedRequest
from imageright_mcp.client.redact import Redactor
from imageright_mcp.config import WriteMode
from imageright_mcp.errors import get_registry

WRITE_SAFETY = frozenset({"write", "destructive"})
MULTIPART_CONTENT_TYPE = "multipart/form-data; boundary=…"

Action = Literal["execute", "preview", "block"]


@dataclass
class Decision:
    action: Action
    error: dict[str, Any] | None = None
    warnings: list[dict[str, str]] = field(default_factory=list)
    confirm_required: bool = False


def preview_id(request: PreparedRequest) -> str:
    """Hash of the resolved request: method, URL, query, body and file digests (not auth or the
    per-request id), so any change to what would be sent yields a different id."""
    canonical: dict[str, Any] = {
        "operationId": request.operation_id,
        "method": request.method,
        "url": request.url,
        "query": [list(pair) for pair in request.query],
    }
    if request.json is not NO_BODY:
        canonical["json"] = request.json
    if request.text is not None:
        canonical["text"] = request.text
    if request.multipart:
        canonical["multipart"] = [_part_view(part) for part in request.multipart]
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return "pv_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


class PreviewLedger:
    """Preview ids issued by this process; a confirm must name one of them."""

    def __init__(self, limit: int = 1000) -> None:
        self._ids: dict[str, None] = {}
        self.limit = limit

    def issue(self, pid: str) -> None:
        self._ids.pop(pid, None)
        self._ids[pid] = None
        while len(self._ids) > self.limit:
            self._ids.pop(next(iter(self._ids)))

    def __contains__(self, pid: object) -> bool:
        return pid in self._ids


def decide(
    *,
    safety: str,
    write_mode: WriteMode,
    config_dry_run: bool,
    dry_run: bool | None,
    require_confirm: bool,
    confirm: str | None,
    current_preview_id: str,
    ledger: PreviewLedger,
) -> Decision:
    registry = get_registry()
    is_write = safety in WRITE_SAFETY
    warnings: list[dict[str, str]] = []
    if is_write and write_mode == "deny":
        return Decision(
            "block",
            registry.error(
                "IR-3008",
                message=f"writeMode is deny, so this {safety} operation is not allowed.",
                hint="Set IMAGERIGHT_WRITE_MODE=dry-run to preview it, or allow to run it.",
            ),
        )
    if dry_run is True or (config_dry_run and dry_run is not False):
        return Decision("preview")
    if is_write and write_mode == "dry-run":
        if dry_run is False:
            warnings.append(
                registry.warning(
                    "IR-3008",
                    "dryRun=false is ignored because writeMode is dry-run; this is a preview.",
                )
            )
        return Decision("preview", warnings=warnings)
    if config_dry_run and dry_run is False and write_mode != "allow":
        warnings.append(
            registry.warning("IR-3008", "dryRun=false only takes effect when writeMode is allow.")
        )
        return Decision("preview", warnings=warnings)
    if safety == "destructive" and require_confirm:
        if confirm is None:
            warnings.append(
                registry.warning(
                    "IR-3009",
                    "Destructive operation: review this preview, then repeat the identical "
                    "call with confirm set to its previewId.",
                )
            )
            return Decision("preview", warnings=warnings, confirm_required=True)
        if confirm != current_preview_id or confirm not in ledger:
            return Decision(
                "block",
                registry.error(
                    "IR-3009",
                    message="confirm does not match a preview of this exact request.",
                    hint="The request changed since the preview (or the preview came from "
                    "another session). Review the new preview and confirm its previewId.",
                ),
                confirm_required=True,
            )
    return Decision("execute", warnings=warnings)


# ---------------------------------------------------------------------------- previews


def _part_view(part: JsonPart | FilePart) -> dict[str, Any]:
    if isinstance(part, JsonPart):
        return {"name": part.name, "contentType": part.content_type, "json": part.value}
    return {
        "name": part.name,
        "file": str(part.path),
        "contentType": part.content_type,
        "bytes": part.size,
        "sha256": part.sha256,
    }


def body_headers(request: PreparedRequest) -> dict[str, str]:
    if request.multipart:
        return {"Content-Type": MULTIPART_CONTENT_TYPE}
    if request.text is not None:
        return {"Content-Type": "text/plain; charset=utf-8"}
    if request.json is not NO_BODY:
        return {"Content-Type": "application/json"}
    return {}


def curl_command(request: PreparedRequest, headers: Mapping[str, str]) -> str:
    """A copy-pasteable curl line; headers must already be redacted."""
    parts = ["curl", "-X", request.method, shlex.quote(request.full_url)]
    if request.method == "HEAD":
        parts = ["curl", "-I", shlex.quote(request.full_url)]
    for name, value in headers.items():
        if request.multipart and name.lower() == "content-type":
            continue  # curl -F sets the boundary itself
        parts += ["-H", shlex.quote(f"{name}: {value}")]
    for part in request.multipart:
        if isinstance(part, JsonPart):
            payload = json.dumps(part.value, separators=(",", ":"))
            parts += ["-F", shlex.quote(f"{part.name}={payload};type={part.content_type}")]
        else:
            parts += ["-F", shlex.quote(f"{part.name}=@{part.path};type={part.content_type}")]
    if request.text is not None:
        parts += ["--data-binary", shlex.quote(request.text)]
    elif request.json is not NO_BODY:
        parts += ["--data-binary", shlex.quote(json.dumps(request.json, separators=(",", ":")))]
    return " ".join(parts)


def possible_errors(op: Mapping[str, Any]) -> list[str]:
    registry = get_registry()
    codes = [registry.for_native_rest(int(c)) for c in op.get("errors") or []]
    codes += ["IR-2004", "IR-2005", "IR-5004", "IR-5005"]
    return list(dict.fromkeys(codes))


def prerequisites(op: Mapping[str, Any]) -> list[str]:
    items: list[str] = []
    for spec in op["params"]:
        sources = [s for s in spec.get("valueFrom") or [] if s.get("op")]
        if sources:
            names = ", ".join(str(s["op"]) for s in sources)
            items.append(f"{spec['name']} must be a real id, e.g. from {names}.")
    items.extend(str(g) for g in op.get("gotchas") or [])
    return items


def build_preview(
    *,
    request: PreparedRequest,
    op: Mapping[str, Any],
    capability_id: str | None,
    auth_headers: Mapping[str, str],
    validation: Mapping[str, Any],
    route: list[dict[str, Any]],
    warnings: list[dict[str, str]],
    redactor: Redactor,
) -> dict[str, Any]:
    headers = redactor.headers({**request.headers, **auth_headers, **body_headers(request)})
    request_view: dict[str, Any] = {
        "method": request.method,
        "url": request.full_url,
        "headers": headers,
    }
    if request.multipart:
        request_view["multipart"] = [_part_view(p) for p in request.multipart]
    elif request.text is not None:
        request_view["text"] = request.text
    elif request.json is not NO_BODY:
        request_view["json"] = request.json
    preview = {
        "previewId": preview_id(request),
        "operationId": request.operation_id,
        "capabilityId": capability_id,
        "safety": op["safety"],
        "route": route,
        "request": request_view,
        "curl": curl_command(request, headers),
        "validation": dict(validation),
        "prerequisites": prerequisites(op),
        "possibleErrors": possible_errors(op),
        "warnings": warnings,
    }
    redacted: dict[str, Any] = redactor.value(preview)
    return redacted
