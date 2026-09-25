"""The step runner behind every composite tool (phase 2; flows F1 and F9-F16).

A composite runs its flow's steps in order through the shared ``RestClient``, so every request
passes the same validation, write policy, dry-run gate, error mapping and redaction as
``ir_call``:

* Reads always execute (``dryRun=false``): later steps need the ids they return. The one
  exception is the ``dryRun`` config setting, which previews every call; a composite then stops
  at its first read and says why.
* Writes use the tool's ``dryRun`` / ``confirm``. A previewed write yields an ``Unresolved``
  placeholder; any later step that needs it is listed as ``planned`` with the reference it
  would use, so a dry-run shows the full step sequence.

Outcomes, all in the standard envelope:

* done / preview / partial - ``ok: true``, ``data = {status, outputs, steps}``.
* needs-input - ``ok: true``, ``data = {status: "needs-input", needsInput: {input, question,
  options}, steps}``. Raised where a flow says to ask the user; nothing further is sent.
* error - ``ok: false``; the IR error carries ``flowId``, ``failedStep`` and ``steps``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from mcp.types import CallToolResult

from imageright_mcp.client import RestClient
from imageright_mcp.client.pipeline import CallOutcome
from imageright_mcp.config import ConfigError
from imageright_mcp.envelope import internal_error, to_envelope
from imageright_mcp.errors import get_registry
from imageright_mcp.runtime import ConfigureError, Runtime

Json = dict[str, Any]
Status = Literal["done", "preview", "partial", "needs-input", "stopped"]


class Unresolved:
    """A value a previewed or planned step would produce, e.g. ``$step4.value``."""

    def __init__(self, ref: str) -> None:
        self.ref = ref

    def __repr__(self) -> str:
        return self.ref


def has_unresolved(value: Any) -> bool:
    if isinstance(value, Unresolved):
        return True
    if isinstance(value, Mapping):
        return any(has_unresolved(v) for v in value.values())
    if isinstance(value, list | tuple):
        return any(has_unresolved(v) for v in value)
    return False


def render(value: Any) -> Any:
    """``value`` with every placeholder replaced by its reference string (for output)."""
    if isinstance(value, Unresolved):
        return value.ref
    if isinstance(value, Mapping):
        return {k: render(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [render(v) for v in value]
    return value


class NeedsInput(Exception):
    """The flow says to ask the user; the composite stops and returns the question."""

    def __init__(self, input_name: str, question: str, options: list[Any] | None = None) -> None:
        super().__init__(question)
        self.input_name = input_name
        self.question = question
        self.options = options


class FlowFailed(Exception):
    """The flow says to stop with an error. ``extra`` keys are added to the error object."""

    def __init__(self, error: Json, extra: Mapping[str, Any] | None = None) -> None:
        super().__init__(error.get("message"))
        self.error = error
        self.extra = dict(extra or {})


class FlowStopped(Exception):
    """A lookup came back as a preview (config ``dryRun``), so the flow cannot go on."""


def pascal_keys(value: Any) -> Any:
    """``value`` with every object key starting upper-case. REST servers may be configured to
    send camelCase JSON (``id``, ``name``) instead of the model's PascalCase; the composites read
    the PascalCase names, so a lookup must not miss just because of the server's casing."""
    if isinstance(value, list):
        return [pascal_keys(item) for item in value]
    if isinstance(value, dict):
        return {
            (k[:1].upper() + k[1:] if isinstance(k, str) else k): pascal_keys(v)
            for k, v in value.items()
        }
    return value


def fail(code: str, message: str, hint: str | None = None, **extra: Any) -> FlowFailed:
    return FlowFailed(get_registry().error(code, message=message, hint=hint), extra)


@dataclass
class Flow:
    client: RestClient
    flow_id: str
    composite: str
    dry_run: bool | None = None
    confirm: str | None = None
    steps: list[Json] = field(default_factory=list)
    warnings: list[Json] = field(default_factory=list)
    previewed: bool = False
    confirm_required: bool = False
    status: Status | None = None

    # ------------------------------------------------------------------ steps

    async def read(self, step: int, operation_id: str, params: Json | None = None) -> Any:
        """Run a lookup step and return its data; an error stops the flow."""
        outcome = await self.client.call(operation_id, params or {}, dry_run=False)
        self._absorb(outcome)
        record: Json = {"step": step, "op": operation_id, "kind": "read"}
        if outcome.error is not None:
            self.steps.append({**record, "status": "failed", "error": outcome.error["code"]})
            raise FlowFailed(outcome.error, {"failedStep": step})
        if outcome.meta.get("dryRun"):
            self.steps.append({**record, "status": "preview", "preview": _preview(outcome)})
            self.previewed = True
            raise FlowStopped
        if isinstance(outcome.data, list):
            record["matches"] = len(outcome.data)
        self.steps.append({**record, "status": "done"})
        return pascal_keys(outcome.data)

    async def write(
        self,
        step: int,
        operation_id: str,
        params: Json,
        files: dict[str, str] | None = None,
        *,
        ref: str | None = None,
        label: Mapping[str, Any] | None = None,
    ) -> Any:
        """Run (or preview) a write step. Returns its data, or an ``Unresolved`` placeholder
        when it was previewed or cannot be built yet because an earlier write was previewed."""
        placeholder = Unresolved(ref or f"$step{step}.value")
        record: Json = {"step": step, "op": operation_id, "kind": "write", **(label or {})}
        if has_unresolved(params):
            self.steps.append({**record, "status": "planned", "params": render(params)})
            return placeholder
        outcome = await self.client.call(
            operation_id, params, files, dry_run=self.dry_run, confirm=self.confirm
        )
        self._absorb(outcome)
        if outcome.error is not None:
            self.steps.append({**record, "status": "failed", "error": outcome.error["code"]})
            raise FlowFailed(outcome.error, {"failedStep": step})
        if outcome.meta.get("dryRun"):
            self.previewed = True
            self.confirm_required |= bool(outcome.meta.get("confirmRequired"))
            self.steps.append({**record, "status": "preview", "preview": _preview(outcome)})
            return placeholder
        self.steps.append({**record, "status": "done", "result": outcome.data})
        return pascal_keys(outcome.data)

    def plan(
        self,
        step: int,
        operation_id: str,
        params: Json,
        *,
        kind: str = "read",
        label: Mapping[str, Any] | None = None,
    ) -> None:
        """List a step that cannot run yet because it depends on a previewed write."""
        self.steps.append(
            {
                "step": step,
                "op": operation_id,
                "kind": kind,
                **(label or {}),
                "status": "planned",
                "params": render(params),
            }
        )

    def _absorb(self, outcome: CallOutcome) -> None:
        for warning in outcome.meta.get("warnings") or []:
            if warning not in self.warnings:
                self.warnings.append(warning)

    # ------------------------------------------------------------------ envelopes

    def _meta(self) -> Json:
        return {
            "flowId": self.flow_id,
            "composite": self.composite,
            "dryRun": self.previewed,
            "confirmRequired": self.confirm_required,
            "warnings": self.warnings,
        }

    def done(self, outputs: Mapping[str, Any]) -> CallToolResult:
        status: Status = self.status or ("preview" if self.previewed else "done")
        data = {"status": status, "outputs": render(outputs), "steps": self.steps}
        return to_envelope(data=data, meta=self._meta())

    def needs_input(self, exc: NeedsInput) -> CallToolResult:
        question: Json = {"input": exc.input_name, "question": exc.question}
        if exc.options is not None:
            question["options"] = exc.options
        data = {"status": "needs-input", "needsInput": question, "steps": self.steps}
        return to_envelope(data=data, meta=self._meta())

    def stopped(self) -> CallToolResult:
        data = {
            "status": "stopped",
            "reason": (
                "The dryRun setting previews every call, including the lookups this composite "
                "needs to resolve names into ids, so it stopped at the first lookup. Clear the "
                "dryRun setting (the tool's own dryRun still previews the writes)."
            ),
            "steps": self.steps,
        }
        return to_envelope(data=data, meta=self._meta())

    def failed(self, exc: FlowFailed) -> CallToolResult:
        error = {**exc.error, **exc.extra, "flowId": self.flow_id, "steps": self.steps}
        if "failedStep" not in error and self.steps:
            # Raised by a lookup's own check (not found, wrong drawer, ...): the last step run.
            error["failedStep"] = self.steps[-1]["step"]
        return to_envelope(error=error, meta=self._meta())


def _preview(outcome: CallOutcome) -> Any:
    data = outcome.data
    return data.get("preview") if isinstance(data, dict) else data


FlowBody = Callable[[Flow], Awaitable[Mapping[str, Any]]]


async def run_flow(
    runtime: Runtime,
    flow_id: str,
    composite: str,
    body: FlowBody,
    *,
    dry_run: bool | None = None,
    confirm: str | None = None,
) -> CallToolResult:
    """Run ``body`` as one composite and turn its outcome into the envelope."""
    try:
        client = await runtime.client()
    except ConfigureError as exc:
        return to_envelope(error=exc.error)
    except ConfigError as exc:
        return to_envelope(error=get_registry().error("IR-1001", message=str(exc)))
    except Exception as exc:
        return to_envelope(error=internal_error(exc))
    flow = Flow(client, flow_id, composite, dry_run=dry_run, confirm=confirm)
    try:
        outputs = await body(flow)
    except NeedsInput as exc:
        return flow.needs_input(exc)
    except FlowFailed as exc:
        return flow.failed(exc)
    except FlowStopped:
        return flow.stopped()
    except Exception as exc:
        return to_envelope(error=internal_error(exc))
    return flow.done(outputs)
