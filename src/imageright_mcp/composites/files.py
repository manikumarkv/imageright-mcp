"""File composites: F9 search_files, F10 create_file, F11 update_file, F12 merge_files."""

from __future__ import annotations

from imageright_mcp.composites.engine import Flow, Json, NeedsInput, fail
from imageright_mcp.composites.resolve import (
    FIND_FILES,
    MAX_OPTIONS,
    file_option,
    find_files,
    items,
    require_file,
    resolve_drawer_by_code,
    resolve_file_type_name,
)

CREATE_FILE = "rest.v1.files.createFile"
UPDATE_FILE = "rest.v1.files.updateFileProperties"
MERGE_FILES = "rest.v1.files.mergeFiles"


async def search_files(
    flow: Flow,
    *,
    fileNumber: str | None,
    filePattern: str | None,
    drawerCode: str | None,
    isTemp: bool | None,
    isDeleted: bool | None,
) -> Json:
    if not fileNumber and not filePattern:
        raise NeedsInput(
            "fileNumber",
            "Which files? Give a file number (fileNumber) or a number pattern with % as the "
            "wildcard (filePattern).",
        )
    params: Json = {"FileNumberPart1": fileNumber or filePattern}
    if drawerCode:
        drawer = await resolve_drawer_by_code(flow, 1, drawerCode, ask=True)
        params["ParentId"] = drawer["Id"]
    if isTemp is not None:
        params["IsTemporary"] = isTemp
    if isDeleted is not None:
        params["IsDeleted"] = isDeleted
    return {"files": items(await flow.read(2, FIND_FILES, params))}


async def create_file(
    flow: Flow,
    *,
    drawerCode: str,
    description: str,
    fileType: str,
    fileNumber: str | None,
    createdByApplication: str,
    isTemporary: bool,
) -> Json:
    drawer = await resolve_drawer_by_code(flow, 1, drawerCode, ask=True)
    file_type = await resolve_file_type_name(flow, 2, fileType)
    if fileNumber:
        taken = await find_files(flow, 3, fileNumber)
        if taken:
            raise fail(
                "IR-4110",
                f"File number {fileNumber} is already in use; no file was created.",
                "Choose another file number, or leave it out to let the server generate one.",
                existing=[file_option(f) for f in taken[:MAX_OPTIONS]],
            )
    params: Json = {
        "FileTypeId": file_type["Id"],
        "ParentId": drawer["Id"],
        "Name": description,
        "IsTemporary": isTemporary,
        "CreatedByApplication": createdByApplication,
    }
    if fileNumber:
        params["FileNumberPart1"] = fileNumber
    return {"fileId": await flow.write(4, CREATE_FILE, params)}


async def update_file(
    flow: Flow, *, fileNumber: str, newFileNumber: str | None, newDescription: str | None
) -> Json:
    if not newFileNumber and not newDescription:
        raise NeedsInput(
            "newFileNumber",
            f"What should change on file {fileNumber}? Give a new file number (newFileNumber) "
            "and/or a new description (newDescription).",
        )
    file = await require_file(flow, 1, fileNumber, input_name="fileNumber", ask=True)
    if newFileNumber:
        taken = [f for f in await find_files(flow, 2, newFileNumber) if f.get("Id") != file["Id"]]
        if taken:
            raise fail(
                "IR-4110",
                f"File number {newFileNumber} is already used by another file; file "
                f"{fileNumber} was not updated.",
                "Choose a file number no other file uses.",
                existing=[file_option(f) for f in taken[:MAX_OPTIONS]],
            )
    params: Json = {"fileId": file["Id"]}
    if newFileNumber:
        params["FileNumberPart1"] = newFileNumber
    if newDescription:
        params["Name"] = newDescription
    await flow.write(3, UPDATE_FILE, params)
    return {"fileId": file["Id"]}


async def merge_files(flow: Flow, *, sourceFileNumber: str, targetFileNumber: str) -> Json:
    source = await require_file(flow, 1, sourceFileNumber, input_name="sourceFileNumber")
    target = await require_file(flow, 2, targetFileNumber, input_name="targetFileNumber")
    if source["Id"] == target["Id"]:
        raise fail(
            "IR-4107",
            f"File numbers {sourceFileNumber} and {targetFileNumber} are the same file; a file "
            "cannot be merged into itself.",
            "Name two different files.",
        )
    await flow.write(3, MERGE_FILES, {"sourceFileId": source["Id"], "targetFileId": target["Id"]})
    return {"fileId": target["Id"]}
