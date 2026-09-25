"""Response normalization (plan §4.5; M6): pipeline step 8.

Level 1 (every call): SOAP results arrive already converted from XML with the WSDL's PascalCase
names; REST JSON stays as the server sent it. On top of that, .NET ``/Date(ms)/`` values become
ISO-8601, an empty array result is ``[]`` and never ``null``, int64 stays a native int, and the
upstream model name goes into ``meta.shape`` (``rest-v1:DocumentDataResult``, ``soap:Document``).

Level 2 (capability calls only): the core entities File, Folder, Document, Page, Task, Workflow,
Step and User get one canonical camelCase shape, whichever surface answered. Fields a surface does
not report are ``null``. The upstream object is kept per entity under ``raw`` when ``includeRaw``
is set. SOAP ``*Ref`` results (``FileRef``, ``TaskRef``) become the plain id, like REST's int64.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

_DOTNET_DATE = re.compile(r"^/Date\((-?\d+)([+-]\d{2})?(\d{2})?\)/$")
# DateTime.MinValue: .NET's "no date".
_MIN_DATE = "0001-01-01"

ENTITY_FIELDS: dict[str, tuple[str, ...]] = {
    "File": (
        "id",
        "description",
        "fileNumber1",
        "fileNumber2",
        "fileNumber3",
        "fileTypeId",
        "fileTypeName",
        "drawerId",
        "dateCreated",
        "dateLastModified",
        "deleted",
    ),
    "Folder": (
        "id",
        "description",
        "folderTypeId",
        "folderTypeName",
        "fileId",
        "parentId",
        "dateCreated",
        "dateLastModified",
        "deleted",
    ),
    "Document": (
        "id",
        "description",
        "documentTypeId",
        "documentTypeName",
        "documentDate",
        "parentId",
        "pageCount",
        "dateCreated",
        "dateLastModified",
        "deleted",
    ),
    "Page": ("id", "documentId", "pageNumber", "description", "version", "extension", "deleted"),
    "Task": (
        "id",
        "objectId",
        "fileId",
        "pageId",
        "workflowId",
        "workflowName",
        "stepId",
        "stepName",
        "status",
        "priority",
        "description",
        "availableDate",
        "deadline",
        "dateInitiated",
        "assignedToId",
        "lockedById",
        "pageDescription",
    ),
    "Workflow": ("id", "name", "programmaticName", "status"),
    "Step": ("id", "workflowId", "name", "programmaticName", "status", "isStart"),
    "User": ("id", "name", "fullName", "description", "enabled", "externalId"),
}


# ---------------------------------------------------------------------------- level 1


def _dotnet_date(match: re.Match[str]) -> str:
    millis = int(match.group(1))
    moment = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=millis)
    if match.group(2):
        hours, minutes = int(match.group(2)), int(match.group(3) or 0)
        offset = timedelta(hours=hours, minutes=minutes if hours >= 0 else -minutes)
        moment = moment.astimezone(timezone(offset))
    return moment.isoformat()


def level1(value: Any) -> Any:
    """Dates to ISO-8601, recursively; everything else unchanged (ints stay ints)."""
    if isinstance(value, str):
        match = _DOTNET_DATE.match(value)
        return _dotnet_date(match) if match else value
    if isinstance(value, list):
        return [level1(v) for v in value]
    if isinstance(value, dict):
        return {k: level1(v) for k, v in value.items()}
    return value


def is_array_type(type_name: str | None) -> bool:
    return bool(type_name) and (
        str(type_name).endswith("[]") or str(type_name).startswith("ArrayOf")
    )


def element_type(type_name: str) -> str:
    if type_name.endswith("[]"):
        return type_name[:-2]
    if type_name.startswith("ArrayOf"):
        return type_name[len("ArrayOf") :]
    return type_name


def rest_type(op: Mapping[str, Any], status: int | None) -> str | None:
    """The catalog's model name for this REST response status (else the first 2xx)."""
    responses: Mapping[str, Mapping[str, Any]] = op.get("responses") or {}
    chosen = responses.get(str(status)) if status is not None else None
    if chosen is None:
        chosen = next((r for s, r in responses.items() if s.startswith("2")), None)
    return str(chosen["type"]) if chosen and chosen.get("type") else None


# ---------------------------------------------------------------------------- level 2 helpers


def _id(value: Any) -> Any:
    """A SOAP reference (``{Id, RefId}``), a ``SecurityID`` (``{Id, ExternalId}``), a nested REST
    object with ``Id``, or already a plain id."""
    if isinstance(value, Mapping):
        if "RefId" in value:
            return value["RefId"]
        return _id(value.get("Id"))
    return value


def _date(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(_MIN_DATE):
        return None
    return value


def _get(obj: Mapping[str, Any], *path: str) -> Any:
    node: Any = obj
    for part in path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(part)
    return node


def _deleted(indicator: Any) -> bool | None:
    if indicator is None:
        return None
    return str(indicator) != "None"


def _not(value: Any) -> bool | None:
    return None if value is None else not value


def _entity(entity: str, /, **values: Any) -> dict[str, Any]:
    fields = ENTITY_FIELDS[entity]
    unknown = set(values) - set(fields)
    if unknown:  # pragma: no cover - guards the mapper table below
        raise KeyError(f"{entity} has no field(s) {sorted(unknown)}")
    return {f: values.get(f) for f in fields}


# ---------------------------------------------------------------------------- mappers


def _file_rest(o: Mapping[str, Any]) -> dict[str, Any]:
    return _entity(
        "File",
        id=o.get("Id"),
        description=o.get("Description"),
        fileNumber1=o.get("FileNumberPart1"),
        fileNumber2=o.get("FileNumberPart2"),
        fileNumber3=o.get("FileNumberPart3"),
        fileTypeId=o.get("FileTypeId"),
        fileTypeName=o.get("FileTypeName"),
        drawerId=o.get("DrawerId"),
        dateCreated=_date(o.get("DateCreated")),
        dateLastModified=_date(o.get("LastModified")),
        deleted=o.get("IsDeleted"),
    )


def _file_soap(o: Mapping[str, Any]) -> dict[str, Any]:
    return _entity(
        "File",
        id=_id(o.get("Id")),
        description=o.get("Description") or o.get("Filename"),
        fileNumber1=o.get("FileNumber1"),
        fileNumber2=o.get("FileNumber2"),
        fileNumber3=o.get("FileNumber3"),
        fileTypeId=_id(_get(o, "ObjType", "Id")),
        fileTypeName=_get(o, "ObjType", "Name"),
        drawerId=_id(o.get("ParentId")),
        dateCreated=_date(o.get("DateCreated")),
        dateLastModified=_date(o.get("DateLastModified")),
        deleted=_deleted(o.get("DeleteIndicator")),
    )


def _folder_rest(o: Mapping[str, Any]) -> dict[str, Any]:
    parent = o.get("ParentFolderId") or o.get("FileId")
    return _entity(
        "Folder",
        id=o.get("Id"),
        description=o.get("Description"),
        folderTypeId=o.get("FolderTypeId"),
        folderTypeName=o.get("FolderTypeName"),
        fileId=o.get("FileId"),
        parentId=parent,
        dateCreated=_date(o.get("DateCreated")),
        dateLastModified=_date(o.get("LastModified")),
        deleted=o.get("IsDeleted"),
    )


def _folder_soap(o: Mapping[str, Any]) -> dict[str, Any]:
    return _entity(
        "Folder",
        id=_id(o.get("Id")),
        description=o.get("Description"),
        folderTypeId=_id(_get(o, "ObjType", "Id")),
        folderTypeName=_get(o, "ObjType", "Name"),
        fileId=None,  # SOAP only reports the direct parent (a file or a folder)
        parentId=_id(o.get("ParentId")),
        dateCreated=_date(o.get("DateCreated")),
        dateLastModified=_date(o.get("DateLastModified")),
        deleted=_deleted(o.get("DeleteIndicator")),
    )


def _document_rest(o: Mapping[str, Any]) -> dict[str, Any]:
    parent = _id(o.get("Folder")) or _id(o.get("File"))
    return _entity(
        "Document",
        id=o.get("Id"),
        description=o.get("Description"),
        documentTypeId=o.get("DocumentTypeId"),
        documentTypeName=o.get("DocumentTypeDescription"),
        documentDate=_date(o.get("DocumentDate")),
        parentId=parent,
        pageCount=o.get("PageCount"),
        dateCreated=_date(o.get("DateCreated")),
        dateLastModified=_date(o.get("DateLastModified")),
        deleted=o.get("Deleted"),
    )


def _document_soap(o: Mapping[str, Any]) -> dict[str, Any]:
    return _entity(
        "Document",
        id=_id(o.get("Id")),
        description=o.get("Description"),
        documentTypeId=_id(_get(o, "ObjType", "Id")),
        documentTypeName=_get(o, "ObjType", "Description") or _get(o, "ObjType", "Name"),
        documentDate=_date(o.get("DocumentDate")),
        parentId=_id(o.get("ParentId")),
        pageCount=o.get("PageCount"),
        dateCreated=_date(o.get("DateCreated")),
        dateLastModified=_date(o.get("DateLastModified")),
        deleted=_deleted(o.get("DeleteIndicator")),
    )


def _page_rest(o: Mapping[str, Any]) -> dict[str, Any]:
    return _entity(
        "Page",
        id=o.get("Id"),
        documentId=o.get("DocumentId"),
        pageNumber=o.get("Pagenumber"),
        description=o.get("Description"),
        version=o.get("Version"),
        extension=o.get("PageExtension"),
        deleted=o.get("Deleted"),
    )


def _page_soap(o: Mapping[str, Any]) -> dict[str, Any]:
    return _entity(
        "Page",
        id=_id(o.get("Id")),
        documentId=None,  # the SOAP Page does not name its document
        pageNumber=o.get("PageNumber"),
        description=o.get("Description"),
        version=o.get("Version"),
        extension=o.get("Format"),
        deleted=o.get("Deleted"),
    )


def _task_rest(o: Mapping[str, Any]) -> dict[str, Any]:
    return _entity(
        "Task",
        id=o.get("Id"),
        objectId=o.get("ObjectId"),
        fileId=o.get("FileId"),
        pageId=o.get("PageNumber"),  # despite the name, a page id (report §11 #13)
        workflowId=o.get("FlowId"),
        workflowName=o.get("FlowName"),
        stepId=o.get("StepId"),
        stepName=o.get("StepName"),
        status=o.get("Status"),
        priority=o.get("Priority"),
        description=o.get("Description"),
        availableDate=_date(o.get("AvailableDate")),
        deadline=_date(o.get("DeadLine")),
        dateInitiated=_date(o.get("DateInitiated")),
        assignedToId=_id(o.get("AssignedTo")),
        lockedById=_id(o.get("LockedBy")),
        pageDescription=o.get("PageDescription"),  # TaskModelV2 only
    )


def _task_soap(o: Mapping[str, Any]) -> dict[str, Any]:
    return _entity(
        "Task",
        id=_id(o.get("TaskId")),
        objectId=o.get("ObjId"),
        fileId=_id(o.get("File")),
        pageId=_id(o.get("PageId")),
        workflowId=None,
        workflowName=None,
        stepId=_id(o.get("StepId")),
        stepName=None,  # SOAP carries programmatic names only (StepId.StepProgrammaticName)
        status=o.get("Status"),
        priority=o.get("Priority"),
        description=o.get("Description"),
        availableDate=_date(o.get("DateAvailable")),
        deadline=_date(o.get("Deadline")),
        dateInitiated=_date(o.get("DateInitiated")),
        assignedToId=_id(o.get("AssignedTo")),
        lockedById=_id(o.get("LockedBy")),
        pageDescription=None,
    )


def _workflow_rest(o: Mapping[str, Any]) -> dict[str, Any]:
    return _entity(
        "Workflow",
        id=o.get("Id"),
        name=o.get("Name"),
        programmaticName=o.get("FlowProgName"),
        status=o.get("Status"),
    )


def _workflow_soap(o: Mapping[str, Any]) -> dict[str, Any]:
    return _entity(
        "Workflow",
        id=_id(o.get("Id")),
        name=o.get("Name"),
        programmaticName=o.get("ProgrammaticName"),
        status=o.get("Status"),
    )


def _step_rest(o: Mapping[str, Any]) -> dict[str, Any]:
    return _entity(
        "Step",
        id=o.get("Id"),
        workflowId=o.get("FlowId"),
        name=o.get("Name"),
        programmaticName=o.get("ProgName"),
        status=o.get("Status"),
        isStart=o.get("IsStart"),
    )


def _step_soap(o: Mapping[str, Any]) -> dict[str, Any]:
    return _entity(
        "Step",
        id=_id(o.get("Id")),
        workflowId=_id(o.get("Flowid")),
        name=o.get("Name"),
        programmaticName=o.get("ProgrammaticName"),
        status=o.get("Status"),
        isStart=None,
    )


def _user_account2(o: Mapping[str, Any]) -> dict[str, Any]:
    return _entity(
        "User",
        id=o.get("Id"),
        name=o.get("Name"),
        fullName=o.get("FriendlyName"),
        description=o.get("Description"),
        enabled=o.get("Enabled"),
        externalId=o.get("ExternalId"),
    )


def _user_account_v1(o: Mapping[str, Any]) -> dict[str, Any]:
    return _entity(
        "User",
        id=o.get("Id"),
        name=o.get("Name"),
        fullName=o.get("FullName"),
        enabled=_not(o.get("Disabled")),
    )


def _user_soap(o: Mapping[str, Any]) -> dict[str, Any]:
    account = o.get("SecurityAccount") or {}
    return _entity(
        "User",
        id=_id(account.get("Id")),
        name=account.get("Name"),
        fullName=o.get("FullName"),
        description=account.get("Description"),
        enabled=_not(account.get("Disabled")),
        externalId=_get(account, "Id", "ExternalId"),
    )


def _authorized_user_soap(o: Mapping[str, Any]) -> dict[str, Any]:
    return _user_soap(o.get("User") or {})


Mapper = Callable[[Mapping[str, Any]], dict[str, Any]]

# (surface family, upstream model) -> (entity, mapper). "rest" covers v1 and v2 where the model
# is identical in both; a surface-specific key wins (v1 and v2 UserAccount differ).
MAPPERS: dict[tuple[str, str], tuple[str, Mapper]] = {
    ("rest", "FileDataResult"): ("File", _file_rest),
    ("soap", "File"): ("File", _file_soap),
    ("rest", "FolderDataResult"): ("Folder", _folder_rest),
    ("soap", "Folder"): ("Folder", _folder_soap),
    ("rest", "DocumentDataResult"): ("Document", _document_rest),
    ("soap", "Document"): ("Document", _document_soap),
    ("rest", "PageModel"): ("Page", _page_rest),
    ("soap", "Page"): ("Page", _page_soap),
    ("rest", "TaskModel"): ("Task", _task_rest),
    ("rest", "TaskModelV2"): ("Task", _task_rest),
    ("soap", "Task"): ("Task", _task_soap),
    ("rest", "WorkflowData"): ("Workflow", _workflow_rest),
    ("soap", "Workflow"): ("Workflow", _workflow_soap),
    ("rest", "StepData"): ("Step", _step_rest),
    ("soap", "Step"): ("Step", _step_soap),
    ("rest", "UserAccount2"): ("User", _user_account2),
    ("rest-v2", "UserAccount"): ("User", _user_account2),
    ("rest-v1", "UserAccount"): ("User", _user_account_v1),
    ("soap", "User"): ("User", _user_soap),
    ("soap", "AuthorizedStepUser"): ("User", _authorized_user_soap),
}


def mapper_for(surface: str, type_name: str) -> tuple[str, Mapper] | None:
    family = "soap" if surface == "soap" else "rest"
    return MAPPERS.get((surface, type_name)) or MAPPERS.get((family, type_name))


# ---------------------------------------------------------------------------- entry point


def normalize(
    data: Any,
    *,
    surface: str,
    type_name: str | None,
    canonical: bool,
    include_raw: bool,
) -> tuple[Any, dict[str, Any]]:
    """Normalize one successful result. Returns ``(data, meta additions)``."""
    meta: dict[str, Any] = {"normalization": "level1"}
    if type_name:
        meta["shape"] = f"{surface}:{type_name}"
    data = level1(data)
    if data is None and is_array_type(type_name):
        data = []
    if type_name and "Notes" in type_name and _has_empty_notes(data):
        meta["note"] = (
            "The server returns a notes container with Id -1 when there are no notes; "
            "it is kept as sent."
        )
    if not canonical or not type_name:
        if include_raw:
            meta["rawNote"] = (
                "includeRaw applies to canonical entities; this result is already the "
                "upstream payload (Level 1)."
            )
        return data, meta

    paging: dict[str, Any] | None = None
    shape = type_name
    if shape.startswith("PageResultOf") and isinstance(data, Mapping):
        paging = {"count": data.get("Count"), "nextPageLink": data.get("NextPageLink")}
        data = data.get("Items") or []
        shape = shape[len("PageResultOf") :] + "[]"
    element = element_type(shape)
    if surface == "soap" and element.endswith("Ref") and isinstance(data, Mapping):
        meta["normalization"] = "level2"
        return _id(data), meta
    found = mapper_for(surface, element)
    if found is None:
        if include_raw:
            meta["rawNote"] = f"{element} has no canonical entity; data is the upstream payload."
        return data, meta
    entity, mapper = found

    def convert(item: Any) -> Any:
        if not isinstance(item, Mapping):
            return item
        out = mapper(item)
        if include_raw:
            out["raw"] = item
        return out

    meta["normalization"] = "level2"
    meta["entity"] = entity
    if paging is not None:
        meta["paging"] = paging
    if isinstance(data, list):
        return [convert(item) for item in data], meta
    return convert(data), meta


def _has_empty_notes(data: Any) -> bool:
    items = data if isinstance(data, list) else [data]
    return any(isinstance(i, Mapping) and i.get("Id") == -1 for i in items)
