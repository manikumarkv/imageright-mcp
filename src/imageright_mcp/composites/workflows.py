"""Workflow composites: F17 find_workflows, F18 find_steps. Both are read-only; they turn the
names F1 create_task takes into the workflows and steps behind them."""

from __future__ import annotations

from imageright_mcp.composites.engine import Flow, Json, NeedsInput
from imageright_mcp.composites.resolve import (
    GET_WORKFLOWS,
    items,
    named,
    names,
    production_steps,
    resolve_workflow,
)

WORKFLOW_KEYS = ("Id", "Name", "FlowProgName", "Status")


async def find_workflows(flow: Flow, *, workflowName: str | None) -> Json:
    workflows = items(await flow.read(1, GET_WORKFLOWS))
    if not workflowName:
        return {"workflows": workflows}
    matches = named(workflows, workflowName)
    if not matches:
        raise NeedsInput(
            "workflowName",
            f"No workflow is named {workflowName}, or you have no rights on it. Which workflow "
            "do you mean?",
            names(workflows),
        )
    return {"workflows": matches}


async def find_steps(flow: Flow, *, workflowName: str, stepName: str | None) -> Json:
    workflow = await resolve_workflow(flow, 1, workflowName)
    steps = await production_steps(flow, 2, workflow)
    if stepName:
        matches = named(steps, stepName)
        if not matches:
            raise NeedsInput(
                "stepName",
                f"Workflow {workflow.get('Name', workflowName)} has no step named {stepName}. "
                "Which step do you mean?",
                names(steps),
            )
        steps = matches
    summary = {k: workflow[k] for k in WORKFLOW_KEYS if k in workflow}
    return {"workflow": summary, "steps": steps}
