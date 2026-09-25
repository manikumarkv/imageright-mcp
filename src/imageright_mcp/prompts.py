"""MCP prompts: ready-made test scenarios that walk the model through the tools.

A prompt only renders instructions; the model then calls the tools. Every prompt that writes
previews first (``dryRun: true``) and waits for the user's go-ahead, so running a test twice
never creates duplicates by accident.
"""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

# Shared rules appended to every prompt.
RULES = """
Rules for every step:
- Every tool returns an envelope: ok, data, error, meta. Report ok / error.code for each call.
- If data.status is "needs-input", show data.needsInput.question and its options to me and wait
  for my answer. Never guess a value.
- If a call fails, run ir_explain_error with the IR code from error.code and tell me what it
  means and what to do. Do not retry a failed write on your own.
"""

# Extra rules for prompts that write.
WRITE_RULES = """
Writing safely:
- First call the write tool with dryRun: true. Show me the preview (data.steps and every
  planned request), then stop and ask me to confirm.
- Only after I confirm, call it again with the same arguments and dryRun: false. If the
  preview's meta.confirmRequired is true, also pass confirm: <the previewId from the preview>.
- If the second call still comes back as a preview (meta.dryRun is true), the server's
  writeMode does not allow writes. Tell me to set IMAGERIGHT_WRITE_MODE=allow and restart the
  server; do not call again.
"""


def _optional(label: str, value: str | None) -> str:
    return f"{label}: {value}" if value else f"{label}: (not given)"


def register_prompts(server: MCPServer) -> None:
    @server.prompt(
        name="smoke_test",
        title="Smoke test the ImageRight connection",
        description=(
            "Read-only check of the whole setup: configuration, connection, workflows and "
            "(optionally) one file search. Reports pass / fail per step."
        ),
    )
    def smoke_test(
        fileNumber: Annotated[
            str | None, Field(description="A file number to search for (optional).")
        ] = None,
    ) -> str:
        file_step = (
            f'5. ir_search_files with fileNumber "{fileNumber}". Report how many files match.'
            if fileNumber
            else "5. Skip the file search (no fileNumber was given) and mark it as skipped."
        )
        return f"""Run a read-only smoke test of the ImageRight MCP server. Do not call any
tool that writes.

1. ir_get_config. Report the ImageRight version, the configured surfaces and the writeMode.
2. ir_test_connection. Report whether each surface is reachable and signed in.
3. ir_find_workflows with no arguments. Report how many workflows are returned and their names.
4. ir_find_steps for the first workflow from step 3. Report its step names.
{file_step}

Finish with a table: step, tool, result (pass / fail / skipped), and a one-line note. Stop at
the first failure only if later steps cannot run without it.
{RULES}"""

    @server.prompt(
        name="create_task_guided",
        title="Create a workflow task, step by step",
        description=(
            "Find the workflow and step by name, preview the task, and create it only after you "
            "confirm."
        ),
    )
    def create_task_guided(
        fileNumber: Annotated[str, Field(description="Number of the file the task is about.")],
        workflowName: Annotated[
            str | None, Field(description="Workflow name; omit to choose from a list.")
        ] = None,
        stepName: Annotated[
            str | None, Field(description="Step name; omit to choose from a list.")
        ] = None,
    ) -> str:
        return f"""Create a workflow task in ImageRight for file {fileNumber}.

Inputs:
- fileNumber: {fileNumber}
- {_optional("workflowName", workflowName)}
- {_optional("stepName", stepName)}

1. ir_find_workflows{f' with workflowName "{workflowName}"' if workflowName else ""}. If no
   workflowName was given, list the workflow names and ask me which one to use.
2. ir_find_steps with the chosen workflowName{f' and stepName "{stepName}"' if stepName else ""}.
   If no stepName was given, list the step names and ask me which one to use.
3. ir_create_task with workflowName, stepName and fileNumber "{fileNumber}". Ask me whether to
   add a description, an assignee or a priority (default 5) before the preview.
4. Report the new taskId and stepId.
{WRITE_RULES}{RULES}"""

    @server.prompt(
        name="find_documents_in_file",
        title="List the documents in a file",
        description="Search one file (optionally one folder of it) and list its documents.",
    )
    def find_documents_in_file(
        fileNumber: Annotated[str, Field(description="Number of the file to search in.")],
        folderName: Annotated[
            str | None, Field(description="Folder to narrow the search to (optional).")
        ] = None,
    ) -> str:
        folder = f' and folderName "{folderName}"' if folderName else ""
        return f"""List the documents in ImageRight file {fileNumber}. Read-only.

1. ir_search_files with fileNumber "{fileNumber}". Confirm exactly one file matches; if not,
   show me the matches and stop.
2. ir_find_documents with fileNumber "{fileNumber}"{folder}.
3. Show the documents as a table: Id, description, document type, document date, page count.
   Say how many there are; zero documents is a valid result, not an error.
{RULES}"""

    @server.prompt(
        name="upload_document_guided",
        title="Upload a PDF as a document, step by step",
        description=(
            "Preview uploading a PDF into a file's folder as a new document, and upload it only "
            "after you confirm."
        ),
    )
    def upload_document_guided(
        fileNumber: Annotated[str, Field(description="Number of the target file.")],
        drawerCode: Annotated[str, Field(description="Drawer code of the file.")],
        folderTypeName: Annotated[str, Field(description="Folder type name inside the file.")],
        docTypeCode: Annotated[str, Field(description="Document type code of the new document.")],
        pdfFile: Annotated[str, Field(description="Path of the PDF on the server's machine.")],
        identifier: Annotated[
            str | None, Field(description="Description of the new document (optional).")
        ] = None,
    ) -> str:
        return f"""Upload a PDF into ImageRight as a new document.

Inputs:
- fileNumber: {fileNumber}
- drawerCode: {drawerCode}
- folderTypeName: {folderTypeName}
- docTypeCode: {docTypeCode}
- pdfFile: {pdfFile}
- {_optional("identifier", identifier)}

1. If no identifier was given, ask me for the document description to use.
2. ir_upload_document with the inputs above. Note from the preview whether the file or the
   folder would be created (forceCreate defaults to true) and point that out to me.
3. Report the documentId, batchId and the number of pages uploaded. If a page failed, list
   which pages succeeded and which one failed.
{WRITE_RULES}{RULES}"""

    @server.prompt(
        name="explain_error",
        title="Explain an ImageRight error",
        description="Explain an IR code, a native ImageRight code, an HTTP status or fault text.",
    )
    def explain_error(
        errorCode: Annotated[
            str,
            Field(description="IR-xxxx code, native code, HTTP status or SOAP fault text."),
        ],
    ) -> str:
        return f"""Explain this ImageRight error: {errorCode}

1. ir_explain_error with query "{errorCode}".
2. In plain words, tell me: what it means, the likely cause, whether retrying can help, and
   the next thing I should check or do.
3. If it maps to several possible errors, list them and say how to tell them apart.
{RULES}"""
