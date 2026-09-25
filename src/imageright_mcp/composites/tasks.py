"""F1 create_task: workflow, step, assignee and file number (optionally one document in a named
folder) -> ids, priority check, then POST /api/tasks. Anything missing or ambiguous is asked of
the user, except the folder, which is an error (IR-4001) when missing or ambiguous."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from imageright_mcp.composites.engine import Flow, Json, NeedsInput
from imageright_mcp.composites.resolve import (
    FIND_DOCUMENTS,
    GET_ALLOWED_TYPES,
    MAX_OPTIONS,
    items,
    named,
    names,
    require_file,
    require_folder,
    same,
)

GET_WORKFLOWS = "rest.v1.workflow.getWorkflows"
GET_STEPS = "rest.v1.workflow.getSteps"
GET_STEP_USERS = "rest.v1.workflow.getUsersToAssign"
GET_PRIORITIES = "rest.v1.workflow.getPriorityList"
CREATE_TASK = "rest.v1.tasks.createTask"


def _one(data: Any, name: str, input_name: str, what: str, key: str = "Name") -> Json:
    matches = named(data, name, key)
    if len(matches) == 1:
        return matches[0]
    question = (
        f"No {what} is named {name}. Which {what} do you mean?"
        if not matches
        else f"{len(matches)} {what}s are named {name}. Which one do you mean?"
    )
    raise NeedsInput(input_name, question, names(data, key))


def _document_option(doc: Json) -> Json:
    keys = ("Id", "Description", "DocumentDate", "DocumentTypeDescription", "PageCount")
    return {k: doc[k] for k in keys if k in doc}


async def create_task(
    flow: Flow,
    *,
    workflowName: str,
    stepName: str,
    fileNumber: str,
    description: str | None,
    assigneeUsername: str | None,
    targetDocumentType: str | None,
    folderName: str | None,
    identifier: str | None,
    priority: int,
    availableDate: str | None,
) -> Json:
    workflow = _one(await flow.read(1, GET_WORKFLOWS), workflowName, "workflowName", "workflow")
    steps = await flow.read(2, GET_STEPS, {"flowId": workflow["Id"], "flag": "Production"})
    step = _one(steps, stepName, "stepName", "step")

    user: Json | None = None
    if assigneeUsername:
        users = await flow.read(3, GET_STEP_USERS, {"stepId": step["Id"]})
        user = _one(users, assigneeUsername, "assigneeUsername", "assignable user")

    allowed = await flow.read(4, GET_PRIORITIES, {"stepId": step["Id"]})
    choices = [p for p in allowed if isinstance(p, int)] if isinstance(allowed, list) else []
    if priority not in choices:
        raise NeedsInput(
            "priority",
            f"Priority {priority} is not allowed on step {stepName}. Which priority should the "
            "task have?",
            sorted(choices),
        )

    file = await require_file(flow, 5, fileNumber, input_name="fileNumber", ask=True)
    target_id = file["Id"]

    if targetDocumentType:
        if not folderName:
            raise NeedsInput(
                "folderName",
                "Documents live under a folder. Which folder of the file holds the "
                f"{targetDocumentType} document?",
            )
        folder = await require_folder(flow, 6, file["Id"], folderName, fileNumber)
        types = await flow.read(7, GET_ALLOWED_TYPES, {"objectId": file["Id"]})
        doc_type = _one(types, targetDocumentType, "targetDocumentType", "document type")
        found = await flow.read(
            8,
            FIND_DOCUMENTS,
            {"FileId": file["Id"], "ParentId": folder["Id"], "DocumentTypeIds": [doc_type["Id"]]},
        )
        target_id = _pick_document(items(found), targetDocumentType, folderName, identifier)["Id"]

    params: Json = {
        "ObjectId": target_id,
        "StepId": step["Id"],
        "Priority": priority,
        "AvailableDate": availableDate or datetime.now(UTC).isoformat(timespec="seconds"),
    }
    if user is not None and user.get("Id") is not None:
        params["UserId"] = user["Id"]
    if description:
        params["Description"] = description
    task = await flow.write(9, CREATE_TASK, params, ref="$step9.Id")
    task_id = task.get("Id") if isinstance(task, dict) else task
    return {"taskId": task_id, "stepId": step["Id"]}


def _pick_document(
    docs: list[Json], type_name: str, folder_name: str, identifier: str | None
) -> Json:
    if len(docs) == 1:
        return docs[0]
    if not docs:
        raise NeedsInput(
            "targetDocumentType",
            f"Folder {folder_name} has no {type_name} documents. Which document type did you mean?",
        )
    if identifier:
        matches = [d for d in docs if same(d.get("Description"), identifier)]
        if len(matches) == 1:
            return matches[0]
        question = (
            f"{len(docs)} {type_name} documents are in folder {folder_name} and "
            f"{'none' if not matches else 'several'} has the description {identifier}. "
            "Which one is the target?"
        )
    else:
        question = (
            f"{len(docs)} {type_name} documents are in folder {folder_name}. Which one is the "
            "target? Pass its description as identifier."
        )
    raise NeedsInput("identifier", question, [_document_option(d) for d in docs[:MAX_OPTIONS]])
