"""Document composites: F13 move_file_content, F14 find_documents, F15 create_document, and the
file / folder resolve-or-create path (steps 1-7) that F15 and F16 share."""

from __future__ import annotations

from datetime import date
from typing import Any

from imageright_mcp.composites.engine import Flow, Json, NeedsInput, Unresolved, fail
from imageright_mcp.composites.resolve import (
    FIND_DOCUMENTS,
    FIND_FOLDERS,
    GET_ALLOWED_TYPES,
    assert_file_in_drawer,
    items,
    require_file,
    require_folder,
    resolve_document_type_code,
    resolve_drawer_by_code,
    resolve_file_by_number,
    resolve_file_type_code,
    resolve_folder,
    resolve_folder_type_name,
    resolve_type_codes,
)

MOVE_DOCUMENTS = "rest.v1.documents.moveDocument"
COPY_DOCUMENTS = "rest.v2.documents.copyDocumentV2"
CREATE_FILE = "rest.v1.files.createFile"
CREATE_FOLDER = "rest.v1.folders.createFolder"
CREATE_DOCUMENT = "rest.v1.documents.createDocument"

ALL_TYPES = "All"


# ------------------------------------------------------------------------------ F13


async def move_file_content(
    flow: Flow,
    *,
    sourceFileNumber: str,
    targetFileNumber: str,
    targetFolderName: str,
    documentTypes: list[str],
    mode: str,
) -> Json:
    if mode not in {"move", "copy"}:
        raise fail("IR-3006", f'mode must be "move" or "copy", not {mode!r}.')
    if not documentTypes or (ALL_TYPES in documentTypes and len(documentTypes) > 1):
        raise fail(
            "IR-3006",
            'documentTypes must be ["All"] or a list of document type codes without "All".',
        )
    source = await require_file(flow, 1, sourceFileNumber, input_name="sourceFileNumber")
    target = await require_file(flow, 2, targetFileNumber, input_name="targetFileNumber")
    folder = await require_folder(flow, 3, target["Id"], targetFolderName, targetFileNumber)

    wanted: set[Any] | None = None
    if documentTypes != [ALL_TYPES]:
        types = await resolve_type_codes(flow, 4, "Document", documentTypes, "document")
        wanted = {t["Id"] for t in types}
    found = items(await flow.read(4, FIND_DOCUMENTS, {"FileId": source["Id"], "Deleted": False}))
    docs = [d for d in found if wanted is None or d.get("DocumentTypeId") in wanted]
    doc_ids = [d["Id"] for d in docs if d.get("Id") is not None]
    outputs: Json = {"moveResult": None, "copyResult": None, "documentIds": doc_ids}
    if not doc_ids:
        flow.status = "done"
        outputs["note"] = "No document of the requested types is in the source file."
        return outputs

    if mode == "move":
        result = await flow.write(
            5, MOVE_DOCUMENTS, {"DocumentIds": doc_ids, "TargetParentId": folder["Id"]}
        )
        outputs["moveResult"] = result
        failed: list[Any] = []
        if isinstance(result, dict):
            failed = list(result.get("FailedDocumentMoves") or [])
            if result.get("Error"):
                outputs["error"] = result["Error"]
                flow.status = "partial"
    else:
        result = await flow.write(
            6, COPY_DOCUMENTS, {"DocumentIds": doc_ids, "TargetId": folder["Id"]}
        )
        outputs["copyResult"] = result
        failed = list(result.get("FailedObjects") or {}) if isinstance(result, dict) else []
    if isinstance(result, dict):
        failed_keys = {str(f) for f in failed}
        outputs["transferred"] = [i for i in doc_ids if str(i) not in failed_keys]
        outputs["failed"] = failed
        if failed:
            flow.status = "partial"
    return outputs


# ------------------------------------------------------------------------------ F14


async def find_documents(
    flow: Flow,
    *,
    fileNumber: str,
    drawerCode: str | None,
    folderName: str | None,
    docTypeCodes: list[str] | None,
    identifier: str | None,
) -> Json:
    file = await require_file(flow, 1, fileNumber, input_name="fileNumber")
    if drawerCode:
        drawer = await resolve_drawer_by_code(flow, 2, drawerCode)
        assert_file_in_drawer(file, drawer, fileNumber, drawerCode)
    params: Json = {"FileId": file["Id"], "Deleted": False}
    if folderName:
        folder = await require_folder(flow, 3, file["Id"], folderName, fileNumber)
        params["ParentId"] = folder["Id"]
    if docTypeCodes:
        types = await resolve_type_codes(flow, 4, "Document", docTypeCodes, "document")
        params["DocumentTypeIds"] = [t["Id"] for t in types]
    # Description is not sent: whether the server matches it exactly or as a substring is
    # unconfirmed, and filtering here gives the contains match F14 asks for either way.
    docs = items(await flow.read(5, FIND_DOCUMENTS, params))
    if identifier:
        needle = identifier.strip().casefold()
        docs = [d for d in docs if needle in str(d.get("Description") or "").casefold()]
    return {"documents": docs}


# ------------------------------------------------------------------------------ F15 / F16


async def ensure_folder(
    flow: Flow,
    *,
    fileNumber: str,
    drawerCode: str | None,
    folderName: str,
    forceCreate: bool,
    fileTypeCode: str | None,
    createdByApplication: str | None,
    fileDescription: str,
    folderTypeName: str | None,
) -> Any:
    """Steps 1-7 of F15 / F16: find (or, with forceCreate, create) the file and the folder that
    receives the document. Returns the folder id, or a placeholder when a create was previewed.
    """
    file = await resolve_file_by_number(
        flow, 1, fileNumber, input_name="fileNumber", allow_missing=forceCreate
    )
    file_id: Any
    if file is None:
        # File-creation path: everything it needs is asked for before any of its requests.
        if not drawerCode:
            raise NeedsInput(
                "drawerCode",
                f"No file has number {fileNumber}. Which drawer should the new file go in?",
            )
        if not createdByApplication:
            raise NeedsInput(
                "createdByApplication",
                f"File {fileNumber} must be created first. Which application name should be "
                "recorded as its creator?",
            )
        if not folderTypeName:
            raise NeedsInput(
                "folderTypeName",
                f"Folder {folderName} must be created in the new file. Which folder type should "
                "it have?",
            )
        drawer = await resolve_drawer_by_code(flow, 2, drawerCode)
        file_type = await resolve_file_type_code(flow, 3, fileTypeCode or drawerCode)
        file_id = await flow.write(
            4,
            CREATE_FILE,
            {
                "FileTypeId": file_type["Id"],
                "ParentId": drawer["Id"],
                "Name": fileDescription,
                "FileNumberPart1": fileNumber,
                "IsTemporary": False,
                "CreatedByApplication": createdByApplication,
            },
        )
    else:
        if drawerCode:
            drawer = await resolve_drawer_by_code(flow, 2, drawerCode)
            assert_file_in_drawer(file, drawer, fileNumber, drawerCode)
        file_id = file["Id"]

    folder: Json | None = None
    if isinstance(file_id, Unresolved):
        # A new file has no folders, so the folder-creation path always follows.
        flow.plan(5, FIND_FOLDERS, {"FileId": file_id, "Description": folderName})
    else:
        folder = await resolve_folder(
            flow, 5, file_id, folderName, fileNumber, allow_missing=forceCreate
        )
    if folder is not None:
        return folder["Id"]

    if not folderTypeName:
        raise NeedsInput(
            "folderTypeName",
            f"File {fileNumber} has no folder {folderName}. Which folder type should the new "
            "folder have?",
        )
    folder_type_id: Any
    if isinstance(file_id, Unresolved):
        flow.plan(6, GET_ALLOWED_TYPES, {"objectId": file_id})
        folder_type_id = Unresolved("$step6.Id")
    else:
        folder_type = await resolve_folder_type_name(flow, 6, file_id, folderTypeName, fileNumber)
        folder_type_id = folder_type["Id"]
    return await flow.write(
        7,
        CREATE_FOLDER,
        {"FolderTypeId": folder_type_id, "ParentId": file_id, "Description": folderName},
    )


async def create_document_in(
    flow: Flow, folder_id: Any, docTypeCode: str, description: str, documentDate: str | None
) -> Any:
    """Steps 8-9 of F15 / F16: resolve the document type code, then create the document."""
    doc_type = await resolve_document_type_code(flow, 8, docTypeCode)
    return await flow.write(
        9,
        CREATE_DOCUMENT,
        {
            "ParentId": folder_id,
            "DocumentTypeId": doc_type["Id"],
            "Description": description,
            "DocumentDate": documentDate or date.today().isoformat(),
        },
    )


async def create_document(
    flow: Flow,
    *,
    fileNumber: str,
    folderName: str,
    docTypeCode: str,
    description: str,
    drawerCode: str | None,
    documentDate: str | None,
    forceCreate: bool,
    fileTypeCode: str | None,
    createdByApplication: str | None,
    fileDescription: str,
    folderTypeName: str | None,
) -> Json:
    folder_id = await ensure_folder(
        flow,
        fileNumber=fileNumber,
        drawerCode=drawerCode,
        folderName=folderName,
        forceCreate=forceCreate,
        fileTypeCode=fileTypeCode,
        createdByApplication=createdByApplication,
        fileDescription=fileDescription,
        folderTypeName=folderTypeName,
    )
    document_id = await create_document_in(flow, folder_id, docTypeCode, description, documentDate)
    return {"documentId": document_id}
