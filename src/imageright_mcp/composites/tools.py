"""The nine phase-2 composite tools (flows F1, F9-F16 in annotations/flows.yaml).

Argument names are the flows' input names. Every tool returns the standard envelope; see
``engine`` for the done / preview / needs-input / error shapes.
"""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import Field

from imageright_mcp.composites.documents import (
    create_document,
    find_documents,
    move_file_content,
)
from imageright_mcp.composites.engine import Flow, Json, run_flow
from imageright_mcp.composites.files import create_file, merge_files, search_files, update_file
from imageright_mcp.composites.tasks import create_task
from imageright_mcp.composites.upload import upload_document
from imageright_mcp.runtime import Runtime

WRITE_NOTE = (
    " Lookups run for real; writes follow writeMode (previewed by default; dryRun=true always "
    "previews), and a preview lists every planned step. When something must be asked of the "
    'user, data.status is "needs-input" and data.needsInput holds the question.'
)

DryRun = Annotated[
    bool | None,
    Field(description="true: preview the writes. false: execute them, if writeMode allows it."),
]
Confirm = Annotated[
    str | None, Field(description="previewId from a prior preview of the identical write.")
]

READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)
DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
)


def register_composite_tools(server: MCPServer, runtime: Runtime) -> None:
    @server.tool(
        name="ir_create_task",
        title="Create a workflow task from names",
        description=(
            "Flow F1. Resolve a workflow name, step name, optional assignee user name and a file "
            "number (optionally one document of targetDocumentType in folder folderName) into "
            "ids, check the priority against the step's allowed list, then create the task. "
            "An unknown or ambiguous workflow, step, user, priority, file or document is asked "
            "of the user; a missing or ambiguous folder is IR-4001." + WRITE_NOTE
        ),
        annotations=WRITE,
    )
    async def ir_create_task(
        workflowName: Annotated[str, Field(description="Display name of the workflow.")],
        stepName: Annotated[str, Field(description="Step the task starts on.")],
        fileNumber: Annotated[str, Field(description="Number of the file the task is about.")],
        description: Annotated[str | None, Field(description="Task description.")] = None,
        assigneeUsername: Annotated[
            str | None, Field(description="User name of the assignee.")
        ] = None,
        targetDocumentType: Annotated[
            str | None,
            Field(description="Document type name; the target becomes a document of this type."),
        ] = None,
        folderName: Annotated[
            str | None,
            Field(description="Folder holding the document; required with targetDocumentType."),
        ] = None,
        identifier: Annotated[
            str | None,
            Field(description="Document description that picks one of several documents."),
        ] = None,
        priority: Annotated[int, Field(description="Priority, 1 to 5.")] = 5,
        availableDate: Annotated[
            str | None, Field(description="ISO date-time the task becomes workable; now.")
        ] = None,
        dryRun: DryRun = None,
        confirm: Confirm = None,
    ) -> CallToolResult:
        async def body(flow: Flow) -> Json:
            return await create_task(
                flow,
                workflowName=workflowName,
                stepName=stepName,
                fileNumber=fileNumber,
                description=description,
                assigneeUsername=assigneeUsername,
                targetDocumentType=targetDocumentType,
                folderName=folderName,
                identifier=identifier,
                priority=priority,
                availableDate=availableDate,
            )

        return await run_flow(runtime, "F1", "create_task", body, dry_run=dryRun, confirm=confirm)

    @server.tool(
        name="ir_search_files",
        title="Search files by number, pattern or drawer",
        description=(
            "Flow F9. Find files by exact number (fileNumber) or a LIKE pattern with % "
            "(filePattern), optionally in one drawer and filtered on temporary / deleted state "
            "(null = no filter). Neither number nor pattern is asked of the user (needs-input); "
            "an unknown drawer code too. Zero matches is an empty list. Read-only."
        ),
        annotations=READ,
    )
    async def ir_search_files(
        fileNumber: Annotated[
            str | None, Field(description="Exact first part of the file number.")
        ] = None,
        filePattern: Annotated[
            str | None, Field(description="Pattern on the first part, % as wildcard.")
        ] = None,
        drawerCode: Annotated[str | None, Field(description="Limit to this drawer.")] = None,
        isTemp: Annotated[
            bool | None, Field(description="Filter on temporary files; null: no filter.")
        ] = False,
        isDeleted: Annotated[
            bool | None, Field(description="Filter on deleted files; null: no filter.")
        ] = False,
    ) -> CallToolResult:
        async def body(flow: Flow) -> Json:
            return await search_files(
                flow,
                fileNumber=fileNumber,
                filePattern=filePattern,
                drawerCode=drawerCode,
                isTemp=isTemp,
                isDeleted=isDeleted,
            )

        return await run_flow(runtime, "F9", "search_files", body)

    @server.tool(
        name="ir_create_file",
        title="Create a file in a drawer",
        description=(
            "Flow F10. Create a file under a drawer: the drawer code and file type name become "
            "ids (unknown ones are asked of the user), and a fileNumber already in use is "
            "IR-4110 before anything is written. Without fileNumber the server generates one."
            + WRITE_NOTE
        ),
        annotations=WRITE,
    )
    async def ir_create_file(
        drawerCode: Annotated[str, Field(description="Drawer that receives the file.")],
        description: Annotated[str, Field(description="Label of the new file (its Name).")],
        fileType: Annotated[str, Field(description="Name of the file type.")],
        createdByApplication: Annotated[
            str, Field(description="Name of the creating application, for auditing.")
        ],
        fileNumber: Annotated[
            str | None, Field(description="First part of the file number, sent as given.")
        ] = None,
        isTemporary: Annotated[bool, Field(description="Mark the file temporary.")] = False,
        dryRun: DryRun = None,
        confirm: Confirm = None,
    ) -> CallToolResult:
        async def body(flow: Flow) -> Json:
            return await create_file(
                flow,
                drawerCode=drawerCode,
                description=description,
                fileType=fileType,
                fileNumber=fileNumber,
                createdByApplication=createdByApplication,
                isTemporary=isTemporary,
            )

        return await run_flow(runtime, "F10", "create_file", body, dry_run=dryRun, confirm=confirm)

    @server.tool(
        name="ir_update_file",
        title="Update a file's number and/or description",
        description=(
            "Flow F11. Find a file by its current number (no match or several matches is asked "
            "of the user) and change its number and/or description. A new number another file "
            "already uses is IR-4110 before anything is written." + WRITE_NOTE
        ),
        annotations=WRITE,
    )
    async def ir_update_file(
        fileNumber: Annotated[str, Field(description="Current first part of the file number.")],
        newFileNumber: Annotated[
            str | None, Field(description="New first part of the file number.")
        ] = None,
        newDescription: Annotated[
            str | None, Field(description="New label of the file (its Name).")
        ] = None,
        dryRun: DryRun = None,
        confirm: Confirm = None,
    ) -> CallToolResult:
        async def body(flow: Flow) -> Json:
            return await update_file(
                flow,
                fileNumber=fileNumber,
                newFileNumber=newFileNumber,
                newDescription=newDescription,
            )

        return await run_flow(runtime, "F11", "update_file", body, dry_run=dryRun, confirm=confirm)

    @server.tool(
        name="ir_merge_files",
        title="Merge one file into another",
        description=(
            "Flow F12. DESTRUCTIVE: merge the source file into the target file; the source is "
            "gone afterwards and this cannot be undone. Each number must match exactly one file "
            "(otherwise IR-4001). Under writeMode allow the merge also needs confirm=<previewId> "
            "from a prior preview." + WRITE_NOTE
        ),
        annotations=DESTRUCTIVE,
    )
    async def ir_merge_files(
        sourceFileNumber: Annotated[str, Field(description="File that is merged away.")],
        targetFileNumber: Annotated[str, Field(description="File that survives.")],
        dryRun: DryRun = None,
        confirm: Confirm = None,
    ) -> CallToolResult:
        async def body(flow: Flow) -> Json:
            return await merge_files(
                flow, sourceFileNumber=sourceFileNumber, targetFileNumber=targetFileNumber
            )

        return await run_flow(runtime, "F12", "merge_files", body, dry_run=dryRun, confirm=confirm)

    @server.tool(
        name="ir_move_file_content",
        title="Move or copy file content by document type",
        description=(
            "Flow F13. Move or copy (default) the source file's documents, optionally only some "
            "document type codes, into a named folder of the target file. Files and folder must "
            "each match exactly once (otherwise IR-4001); an unknown type code is IR-4006. "
            'Partial failures are listed in outputs.failed and set status "partial".' + WRITE_NOTE
        ),
        annotations=WRITE,
    )
    async def ir_move_file_content(
        sourceFileNumber: Annotated[str, Field(description="File the documents come from.")],
        targetFileNumber: Annotated[str, Field(description="File that receives them.")],
        targetFolderName: Annotated[
            str, Field(description="Folder of the target file that receives them.")
        ],
        documentTypes: Annotated[
            list[str] | None,
            Field(description='Document type codes; ["All"] (default) means every document.'),
        ] = None,
        mode: Annotated[str, Field(description='"move" or "copy".')] = "copy",
        dryRun: DryRun = None,
        confirm: Confirm = None,
    ) -> CallToolResult:
        async def body(flow: Flow) -> Json:
            return await move_file_content(
                flow,
                sourceFileNumber=sourceFileNumber,
                targetFileNumber=targetFileNumber,
                targetFolderName=targetFolderName,
                documentTypes=documentTypes if documentTypes is not None else ["All"],
                mode=mode,
            )

        return await run_flow(
            runtime, "F13", "move_file_content", body, dry_run=dryRun, confirm=confirm
        )

    @server.tool(
        name="ir_find_documents",
        title="Find documents in a file",
        description=(
            "Flow F14. List the non-deleted documents of one file, optionally only in one "
            "folder, of certain document type codes, and whose description contains identifier. "
            "The file (and folder) must match exactly once (otherwise IR-4001); a drawerCode the "
            "file is not in is IR-4107; an unknown type code is IR-4006. Read-only."
        ),
        annotations=READ,
    )
    async def ir_find_documents(
        fileNumber: Annotated[str, Field(description="First part of the file number.")],
        drawerCode: Annotated[
            str | None, Field(description="The file must be in this drawer.")
        ] = None,
        folderName: Annotated[str | None, Field(description="Only this folder.")] = None,
        docTypeCodes: Annotated[
            list[str] | None, Field(description="Only these document type codes.")
        ] = None,
        identifier: Annotated[
            str | None, Field(description="Substring of the document description.")
        ] = None,
    ) -> CallToolResult:
        async def body(flow: Flow) -> Json:
            return await find_documents(
                flow,
                fileNumber=fileNumber,
                drawerCode=drawerCode,
                folderName=folderName,
                docTypeCodes=docTypeCodes,
                identifier=identifier,
            )

        return await run_flow(runtime, "F14", "find_documents", body)

    @server.tool(
        name="ir_create_document",
        title="Create a document (creating the file if needed)",
        description=(
            "Flow F15. Create a document in a folder of a file. The file is found by number "
            "(checked against drawerCode when given) and the folder by description; with "
            "forceCreate a missing file and folder are created first, otherwise they are "
            "IR-4001. Unknown type codes are IR-4006." + WRITE_NOTE
        ),
        annotations=WRITE,
    )
    async def ir_create_document(
        fileNumber: Annotated[str, Field(description="File that receives the document.")],
        folderName: Annotated[str, Field(description="Folder (description) in that file.")],
        docTypeCode: Annotated[str, Field(description="Document type code.")],
        description: Annotated[str, Field(description="Label of the new document.")],
        drawerCode: Annotated[
            str | None,
            Field(description="Drawer of the file; required when the file must be created."),
        ] = None,
        documentDate: Annotated[
            str | None, Field(description="ISO business date of the document; today.")
        ] = None,
        forceCreate: Annotated[
            bool, Field(description="Create a missing file and folder first.")
        ] = False,
        fileTypeCode: Annotated[
            str | None, Field(description="File type code of a new file; the drawer code.")
        ] = None,
        createdByApplication: Annotated[
            str | None, Field(description="Creating application, when a file is created.")
        ] = None,
        fileDescription: Annotated[str, Field(description="Label of a new file.")] = "",
        folderTypeName: Annotated[
            str | None, Field(description="Folder type name, when a folder is created.")
        ] = None,
        dryRun: DryRun = None,
        confirm: Confirm = None,
    ) -> CallToolResult:
        async def body(flow: Flow) -> Json:
            return await create_document(
                flow,
                fileNumber=fileNumber,
                folderName=folderName,
                docTypeCode=docTypeCode,
                description=description,
                drawerCode=drawerCode,
                documentDate=documentDate,
                forceCreate=forceCreate,
                fileTypeCode=fileTypeCode,
                createdByApplication=createdByApplication,
                fileDescription=fileDescription,
                folderTypeName=folderTypeName,
            )

        return await run_flow(
            runtime, "F15", "create_document", body, dry_run=dryRun, confirm=confirm
        )

    @server.tool(
        name="ir_upload_document",
        title="Upload a document from a PDF file",
        description=(
            "Flow F16. Resolve or (forceCreate, the default) create the file, the folder (found "
            "and created by folderTypeName) and the document, open a capture batch, then render "
            "each page of a local PDF (inside the allowed file roots) to an image and upload it "
            "as one page, in order. A failed page stops the upload and the error lists the "
            "pages that were uploaded and the one that failed." + WRITE_NOTE
        ),
        annotations=WRITE,
    )
    async def ir_upload_document(
        fileNumber: Annotated[str, Field(description="File that receives the document.")],
        drawerCode: Annotated[str, Field(description="Drawer of the file.")],
        folderTypeName: Annotated[
            str, Field(description="Folder type name, also the folder's description.")
        ],
        docTypeCode: Annotated[str, Field(description="Document type code.")],
        identifier: Annotated[str, Field(description="Label of the new document.")],
        pdfFile: Annotated[str, Field(description="Local PDF path inside the allowed roots.")],
        forceCreate: Annotated[
            bool, Field(description="Create a missing file and folder first.")
        ] = True,
        documentDate: Annotated[
            str | None, Field(description="ISO business date of the document; today.")
        ] = None,
        createdByApplication: Annotated[
            str | None, Field(description="Creating application, when a file is created.")
        ] = None,
        dryRun: DryRun = None,
        confirm: Confirm = None,
    ) -> CallToolResult:
        async def body(flow: Flow) -> Json:
            return await upload_document(
                flow,
                fileNumber=fileNumber,
                drawerCode=drawerCode,
                folderTypeName=folderTypeName,
                docTypeCode=docTypeCode,
                identifier=identifier,
                pdfFile=pdfFile,
                forceCreate=forceCreate,
                documentDate=documentDate,
                createdByApplication=createdByApplication,
            )

        return await run_flow(
            runtime, "F16", "upload_document", body, dry_run=dryRun, confirm=confirm
        )
