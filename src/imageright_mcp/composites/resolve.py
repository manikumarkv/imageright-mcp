"""Name / code / number -> id lookups shared by the composites.

Each helper takes the calling flow's rule for "nothing matched" and "several matched" (ask the
user or stop with an IR error), so one helper serves every flow that uses its lookup.

Matching is exact after trimming and ignoring case. Server-side search fields whose semantics are
unconfirmed (FileNumberPart1 may honour ``%``; folder Description may match a substring) are
re-checked here, so a lookup never picks an object whose number or description differs.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from imageright_mcp.composites.engine import Flow, Json, NeedsInput, fail

FIND_FILES = "rest.v1.files.findFiles"
FIND_FOLDERS = "rest.v1.folders.findFolders"
FIND_DOCUMENTS = "rest.v1.documents.findDocuments"
GET_DRAWERS = "rest.v1.drawers.getDrawers"
GET_TYPES_FOR_CLASS = "rest.v1.objecttypes.getTypesForClass"
GET_ALLOWED_TYPES = "rest.v1.objecttypes.getAllowedTypesForContainer"

# How many candidates a needs-input question lists.
MAX_OPTIONS = 50


def same(value: Any, wanted: str) -> bool:
    return isinstance(value, str) and value.strip().casefold() == wanted.strip().casefold()


def items(data: Any) -> list[Json]:
    """The dict items of a list result (anything else counts as no items)."""
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def named(data: Any, name: str, key: str = "Name") -> list[Json]:
    return [item for item in items(data) if same(item.get(key), name)]


def names(data: Any, key: str = "Name") -> list[str]:
    found = [str(item[key]) for item in items(data) if isinstance(item.get(key), str)]
    return sorted(dict.fromkeys(found))[:MAX_OPTIONS]


def file_option(file: Json) -> Json:
    keys = ("Id", "FileNumberPart1", "Description", "DrawerName", "FileTypeName", "IsTemporary")
    return {k: file[k] for k in keys if k in file}


# ------------------------------------------------------------------------------ files


async def find_files(flow: Flow, step: int, number: str) -> list[Json]:
    """Files whose FileNumberPart1 equals ``number``."""
    data = await flow.read(step, FIND_FILES, {"FileNumberPart1": number})
    return named(data, number, "FileNumberPart1")


async def resolve_file_by_number(
    flow: Flow,
    step: int,
    number: str,
    *,
    input_name: str,
    ask: bool = False,
    allow_missing: bool = False,
) -> Json | None:
    """The single file with this number.

    ``ask`` (F1, F11): no match or several matches is a needs-input question. Otherwise (F12-F16)
    both are IR-4001; with ``allow_missing`` (F15/F16 forceCreate) no match returns None.
    """
    matches = await find_files(flow, step, number)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        if ask:
            raise NeedsInput(
                input_name, f"No file has number {number}. Which file number did you mean?"
            )
        if allow_missing:
            return None
        raise fail(
            "IR-4001",
            f"No file has number {number}.",
            "Check the file number, or search for it with ir_search_files.",
        )
    if ask:
        raise NeedsInput(
            input_name,
            f"File number {number} matches {len(matches)} files. Which one do you mean?",
            [file_option(f) for f in matches[:MAX_OPTIONS]],
        )
    raise fail(
        "IR-4001",
        f"File number {number} matches more than one file.",
        "The flow never guesses between files; ir_search_files lists them.",
        matches=[file_option(f) for f in matches[:MAX_OPTIONS]],
    )


async def require_file(
    flow: Flow, step: int, number: str, *, input_name: str, ask: bool = False
) -> Json:
    """``resolve_file_by_number`` for flows where a missing file never lets the flow go on."""
    file = await resolve_file_by_number(flow, step, number, input_name=input_name, ask=ask)
    if file is None:  # pragma: no cover - only allow_missing returns None
        raise fail("IR-4001", f"No file has number {number}.")
    return file


# ------------------------------------------------------------------------------ drawers


async def resolve_drawer_by_code(
    flow: Flow, step: int, code: str, *, input_name: str = "drawerCode", ask: bool = False
) -> Json:
    """The drawer whose Name is ``code`` (drawers have no separate code field).

    ``ask`` (F9, F10): unknown code -> needs-input offering the codes. Otherwise IR-4001.
    """
    data = await flow.read(step, GET_DRAWERS)
    matches = named(data, code)
    if len(matches) == 1:
        return matches[0]
    if ask:
        question = (
            f"No drawer has code {code}. Which drawer do you mean?"
            if not matches
            else f"Drawer code {code} matches {len(matches)} drawers. Which one do you mean?"
        )
        raise NeedsInput(input_name, question, names(data))
    if not matches:
        raise fail("IR-4001", f"No drawer has code {code}.", "Check the drawer code.")
    raise fail("IR-4001", f"Drawer code {code} matches more than one drawer.")


def assert_file_in_drawer(file: Json, drawer: Json, file_number: str, drawer_code: str) -> None:
    """IR-4107 when the found file's DrawerId is not the drawer's Id."""
    if file.get("DrawerId") != drawer.get("Id"):
        raise fail(
            "IR-4107",
            f"File {file_number} is not in drawer {drawer_code}.",
            "Check the drawer code, or leave it out to accept the file wherever it is.",
            fileDrawer=file.get("DrawerName"),
        )


# ------------------------------------------------------------------------------ folders


async def resolve_folder(
    flow: Flow,
    step: int,
    file_id: Any,
    name: str,
    file_number: str,
    *,
    allow_missing: bool = False,
) -> Json | None:
    """The single folder in the file whose Description is ``name``. No match is IR-4001 (or
    None with ``allow_missing``); several matches are always IR-4001."""
    data = await flow.read(step, FIND_FOLDERS, {"FileId": file_id, "Description": name})
    matches = named(data, name, "Description")
    if len(matches) == 1:
        return matches[0]
    if not matches:
        if allow_missing:
            return None
        raise fail(
            "IR-4001",
            f"File {file_number} has no folder {name}.",
            "Check the folder description; folders are matched on their description.",
        )
    raise fail(
        "IR-4001",
        f"Folder {name} matches more than one folder in file {file_number}.",
        "The flow never guesses between folders.",
    )


async def require_folder(flow: Flow, step: int, file_id: Any, name: str, file_number: str) -> Json:
    folder = await resolve_folder(flow, step, file_id, name, file_number)
    if folder is None:  # pragma: no cover - only allow_missing returns None
        raise fail("IR-4001", f"File {file_number} has no folder {name}.")
    return folder


# ------------------------------------------------------------------------------ types


def type_code_matches(data: Any, code: str) -> list[Json]:
    """Object types whose code is ``code``.

    Which ObjectTypeDataResult field holds a type code is unconfirmed (flows F13-F16); Name is
    tried first, then AutomationId. Change it here once a test server settles it.
    """
    return named(data, code) or named(data, code, "AutomationId")


async def resolve_type_codes(
    flow: Flow, step: int, object_class: str, codes: Iterable[str], kind: str
) -> list[Json]:
    """One type per code, in order; IR-4006 naming every code that matches no type."""
    data = await flow.read(step, GET_TYPES_FOR_CLASS, {"standardObjectClass": object_class})
    found: list[Json] = []
    unknown: list[str] = []
    for code in codes:
        matches = type_code_matches(data, code)
        if len(matches) == 1:
            found.append(matches[0])
        else:
            unknown.append(code)
    if unknown:
        listed = ", ".join(unknown)
        raise fail(
            "IR-4006",
            f"No {kind} type has code {listed}."
            if len(unknown) == 1
            else f"No {kind} type has these codes: {listed}.",
            f"Check the {kind} type code; codes are matched on the type's Name, then its "
            "AutomationId.",
            unknownCodes=unknown,
        )
    return found


async def resolve_document_type_code(flow: Flow, step: int, code: str) -> Json:
    return (await resolve_type_codes(flow, step, "Document", [code], "document"))[0]


async def resolve_file_type_code(flow: Flow, step: int, code: str) -> Json:
    return (await resolve_type_codes(flow, step, "File", [code], "file"))[0]


async def resolve_file_type_name(flow: Flow, step: int, name: str) -> Json:
    """F10: the file type whose Name is ``name``; otherwise ask, listing every file type name."""
    data = await flow.read(step, GET_TYPES_FOR_CLASS, {"standardObjectClass": "File"})
    matches = named(data, name)
    if len(matches) == 1:
        return matches[0]
    raise NeedsInput(
        "fileType",
        f"No single file type is named {name}. Which file type do you mean?",
        names(data),
    )


async def resolve_folder_type_name(
    flow: Flow, step: int, container_id: Any, name: str, file_number: str
) -> Json:
    """The folder type named ``name`` among the types the file's template allows (IR-4006).

    The allowed list mixes folder and document types; the name is assumed to single out the
    folder type (flows F15/F16 mark this as unconfirmed).
    """
    data = await flow.read(step, GET_ALLOWED_TYPES, {"objectId": container_id})
    matches = named(data, name)
    if len(matches) == 1:
        return matches[0]
    raise fail(
        "IR-4006",
        f"File {file_number} allows no folder type named {name}."
        if not matches
        else f"More than one type allowed in file {file_number} is named {name}.",
        "Check the folder type name against the types the file's template allows.",
        allowedTypes=names(data),
    )
