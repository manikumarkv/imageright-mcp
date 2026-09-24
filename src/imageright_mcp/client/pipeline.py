"""Pipeline steps shared by the REST and SOAP clients (plan §4, steps 3-4 and the envelope):
the call outcome, and the validation / write-policy / dry-run gate in front of the transport.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from mcp.types import CallToolResult

from imageright_mcp.client.models import PreparedRequest
from imageright_mcp.client.policy import PreviewLedger, build_preview, decide, preview_id
from imageright_mcp.client.redact import Redactor
from imageright_mcp.client.validator import Validation
from imageright_mcp.config import EffectiveConfig
from imageright_mcp.envelope import to_envelope
from imageright_mcp.errors import get_registry


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


def policy_gate(
    *,
    config: EffectiveConfig,
    ledger: PreviewLedger,
    redactor: Redactor,
    request: PreparedRequest,
    op: Mapping[str, Any],
    meta: dict[str, Any],
    validation: Validation,
    route: list[dict[str, Any]],
    auth_headers: Mapping[str, str],
    confirm: str | None,
    dry_run: bool | None,
) -> CallOutcome | None:
    """Validation, write policy and dry-run for a built request: the outcome to return now
    (validation error, block or preview), or None when the request may be executed.

    ``request`` is what the preview shows and what ``previewId`` hashes; for SOAP that is the
    envelope with the token slot already masked.
    """
    registry = get_registry()
    pid = preview_id(request)
    decision = decide(
        safety=str(op["safety"]),
        write_mode=config.writeMode,
        config_dry_run=config.dryRun,
        dry_run=dry_run,
        require_confirm=config.requireConfirm,
        confirm=confirm,
        current_preview_id=pid,
        ledger=ledger,
    )
    meta["warnings"].extend(decision.warnings)

    def preview() -> dict[str, Any]:
        ledger.issue(pid)
        return build_preview(
            request=request,
            op=op,
            capability_id=meta["capabilityId"],
            auth_headers=auth_headers,
            validation=validation.to_dict(),
            route=route,
            warnings=list(meta["warnings"]),
            redactor=redactor,
        )

    if not validation.ok:
        first = validation.issues[0]
        error = registry.error(first.code, message=first.message)
        error["issues"] = [i.to_dict() for i in validation.issues]
        error["preview"] = preview()
        meta["dryRun"] = decision.action != "execute"
        return CallOutcome(error=error, meta=meta)
    if decision.action == "block":
        assert decision.error is not None
        error = dict(decision.error)
        error["preview"] = preview()
        meta["dryRun"] = True
        return CallOutcome(error=error, meta=meta)
    if decision.action == "preview":
        meta["dryRun"] = True
        meta["confirmRequired"] = decision.confirm_required
        return CallOutcome(data={"preview": preview()}, meta=meta)
    return None
