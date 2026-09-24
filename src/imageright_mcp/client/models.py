"""Transport-agnostic request/response values (plan §4.1).

A ``PreparedRequest`` is exactly what dry-run shows and what ``Transport.send`` executes; tests
assert on it through ``MockTransport``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD"})


class _NoBody:
    def __repr__(self) -> str:
        return "NO_BODY"


NO_BODY: Any = _NoBody()


@dataclass(frozen=True)
class JsonPart:
    """A multipart part carrying JSON (``PageCreateData``, ``PageUpdateData``, ...)."""

    name: str
    value: Any
    content_type: str = "application/json"


@dataclass(frozen=True)
class FilePart:
    """A multipart part streamed from a local file; the bytes never enter tool output."""

    name: str
    path: Path
    size: int
    sha256: str
    content_type: str = "application/octet-stream"

    @property
    def filename(self) -> str:
        return self.path.name


MultipartPart = JsonPart | FilePart


@dataclass(frozen=True)
class PreparedRequest:
    method: str
    url: str
    operation_id: str
    query: tuple[tuple[str, str], ...] = ()
    headers: dict[str, str] = field(default_factory=dict)
    json: Any = NO_BODY
    text: str | None = None
    multipart: tuple[MultipartPart, ...] = ()
    # Retries and 401 replays are only ever allowed for these (plan §4.1).
    idempotent: bool = False
    # 202 on this operation means DataNotReady (native 15), which a read may retry.
    may_be_not_ready: bool = False
    # The catalog types the success body as binary (images, report chunks): write it to disk.
    expects_binary: bool = False
    # The catalog types it as a scalar (token string, id, date) even if sent as octet-stream.
    expects_scalar: bool = False
    request_id: str | None = None

    @property
    def full_url(self) -> str:
        return f"{self.url}?{urlencode(self.query)}" if self.query else self.url

    @property
    def is_soap(self) -> bool:
        return "SOAPAction" in self.headers

    def with_headers(self, extra: dict[str, str]) -> PreparedRequest:
        return replace(self, headers={**self.headers, **extra})


@dataclass(frozen=True)
class BinaryRef:
    """A binary response body written to disk; the envelope carries this, never the bytes."""

    path: str
    bytes: int
    contentType: str
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "bytes": self.bytes,
            "contentType": self.contentType,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class RawResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""
    file: BinaryRef | None = None
    attempts: int = 1
    request_id: str | None = None

    @property
    def content_type(self) -> str:
        for key, value in self.headers.items():
            if key.lower() == "content-type":
                return value
        return ""


class TransportFailure(Exception):
    """The request never produced an HTTP response (DNS, TLS, connect, read, timeout)."""

    def __init__(self, message: str, *, timeout: bool = False) -> None:
        super().__init__(message)
        self.timeout = timeout


def file_digest(path: Path) -> tuple[int, str]:
    """Size and sha256 of a file, read in chunks."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 16):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def is_idempotent(method: str) -> bool:
    return method.upper() in IDEMPOTENT_METHODS
