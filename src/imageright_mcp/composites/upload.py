"""F16 upload_document: resolve or create file, folder and document, open a capture batch, then
render each page of a local PDF to an image (PyMuPDF) and upload it as one page, in order.

The PDF is read through the same allowed-roots check as any other upload (``fileRoots``) and is
opened before the first request, so an unreadable or empty PDF fails before anything is sent.
Page images are rendered into a private temporary directory only when the pages are really
uploaded, and removed afterwards.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pymupdf

from imageright_mcp.client import BuildError, RestClient
from imageright_mcp.composites.documents import create_document_in, ensure_folder
from imageright_mcp.composites.engine import Flow, FlowFailed, Json, Unresolved, fail

CREATE_BATCH = "rest.v1.batches.createBatch"
CREATE_PAGE = "rest.v1.pages.createPage"
BATCH_APPLICATION = "WebSdk"
RENDER_DPI = 200


def _open(path: Path) -> pymupdf.Document:
    return pymupdf.open(path, filetype="pdf")  # type: ignore[no-untyped-call]


def open_pdf(client: RestClient, raw_path: str) -> tuple[Path, int]:
    """The resolved PDF path (inside the allowed roots) and its page count."""
    try:
        path = client.builder.file_part("pdfFile", raw_path).path
    except BuildError as exc:
        raise FlowFailed(exc.error) from None
    try:
        with _open(path) as pdf:
            count = int(pdf.page_count)
    except Exception as exc:
        raise fail(
            "IR-3006",
            f"pdfFile {path.name} could not be read as a PDF ({type(exc).__name__}); nothing "
            "was sent.",
            "Pass a readable, unencrypted PDF.",
        ) from None
    if count < 1:
        raise fail("IR-3006", f"pdfFile {path.name} has no pages; nothing was sent.")
    return path, count


@contextmanager
def rendered_pages(client: RestClient, pdf_path: Path) -> Iterator[list[Path]]:
    """Render every page to a PNG in a temporary directory the upload may read from."""
    with tempfile.TemporaryDirectory(prefix="imageright-pages-") as tmp:
        root = Path(tmp).resolve()
        images: list[Path] = []
        with _open(pdf_path) as pdf:
            for index, page in enumerate(pdf, start=1):
                image = root / f"{pdf_path.stem}-page{index:04d}.png"
                page.get_pixmap(dpi=RENDER_DPI).save(image)
                images.append(image)
        roots = client.builder.file_roots
        roots.append(root)
        try:
            yield images
        finally:
            if root in roots:
                roots.remove(root)


async def upload_document(
    flow: Flow,
    *,
    fileNumber: str,
    drawerCode: str,
    folderTypeName: str,
    docTypeCode: str,
    identifier: str,
    pdfFile: str,
    forceCreate: bool,
    documentDate: str | None,
    createdByApplication: str | None,
) -> Json:
    pdf_path, page_count = open_pdf(flow.client, pdfFile)
    folder_id = await ensure_folder(
        flow,
        fileNumber=fileNumber,
        drawerCode=drawerCode,
        folderName=folderTypeName,
        forceCreate=forceCreate,
        fileTypeCode=None,
        createdByApplication=createdByApplication,
        fileDescription="",
        folderTypeName=folderTypeName,
    )
    document_id = await create_document_in(flow, folder_id, docTypeCode, identifier, documentDate)
    batch_id = await flow.write(10, CREATE_BATCH, {"Application": BATCH_APPLICATION})
    outputs: Json = {"documentId": document_id, "batchId": batch_id, "pageCount": page_count}

    if isinstance(document_id, Unresolved) or isinstance(batch_id, Unresolved):
        for number in range(1, page_count + 1):
            flow.plan(
                11,
                CREATE_PAGE,
                {"DocId": document_id, "BatchId": batch_id, "image0": f"page {number} image"},
                kind="write",
                label={"page": number},
            )
        outputs["pageIds"] = Unresolved("$step11.Id")
        return outputs

    page_ids: list[Any] = []
    with rendered_pages(flow.client, pdf_path) as images:
        for number, image in enumerate(images, start=1):
            try:
                page = await flow.write(
                    11,
                    CREATE_PAGE,
                    {"DocId": document_id, "BatchId": batch_id},
                    {"image0": str(image)},
                    label={"page": number},
                )
            except FlowFailed as exc:
                exc.extra["outputs"] = {**outputs, "pageIds": page_ids}
                exc.extra["pages"] = {
                    "uploaded": [{"page": i, "pageId": p} for i, p in enumerate(page_ids, 1)],
                    "failed": {"page": number, "error": exc.error.get("code")},
                    "notAttempted": list(range(number + 1, page_count + 1)),
                }
                exc.error["message"] = (
                    f"Page {number} of {page_count} failed to upload; pages "
                    f"1-{number - 1} are in the document. {exc.error.get('message')}"
                    if number > 1
                    else f"Page 1 of {page_count} failed to upload; no page is in the document. "
                    f"{exc.error.get('message')}"
                )
                raise
            page_ids.append(page.get("Id") if isinstance(page, dict) else page)
    outputs["pageIds"] = page_ids
    return outputs
