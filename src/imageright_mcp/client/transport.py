"""Transports (plan §4.1): ``Transport.send(PreparedRequest) -> RawResponse``.

``RestTransport`` speaks HTTP through httpx2; ``MockTransport`` answers from a script in tests.
Both hand response bodies to the same ``BodySink``, so binary-to-file behaves identically.
Retries live in ``send_with_retries`` and only ever apply to idempotent requests.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import mimetypes
import ssl
import tempfile
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, BinaryIO, Protocol

import httpx2

from imageright_mcp.client.models import (
    NO_BODY,
    BinaryRef,
    FilePart,
    JsonPart,
    PreparedRequest,
    RawResponse,
    TransportFailure,
)

logger = logging.getLogger(__name__)

TEXT_TYPES = ("application/json", "application/problem+json", "application/xml", "text/")
DATA_NOT_READY = 15
RETRY_STATUSES = frozenset({503})


class Transport(Protocol):
    async def send(self, request: PreparedRequest) -> RawResponse: ...

    async def aclose(self) -> None: ...


def dumps(value: Any) -> bytes:
    """JSON for the wire. Python ints are arbitrary precision, so int64 ids above 2^53 survive."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def is_text_type(content_type: str) -> bool:
    lowered = content_type.split(";", 1)[0].strip().lower()
    return lowered.endswith("+json") or any(lowered.startswith(t) for t in TEXT_TYPES)


class BodySink:
    """Decides where a response body goes: memory (JSON/text/scalars) or a file on disk."""

    def __init__(self, output_dir: Path | None = None) -> None:
        self.output_dir = output_dir or Path(tempfile.gettempdir()) / "imageright-mcp"

    def wants_file(self, request: PreparedRequest, status: int, content_type: str) -> bool:
        if not 200 <= status < 300 or request.method == "HEAD":
            return False
        if is_text_type(content_type) or request.expects_scalar:
            return False
        # The catalog says octet-stream for token strings, ids and dates too; only bodies it
        # types as binary (or leaves untyped, e.g. images) go to disk.
        return request.expects_binary or bool(content_type)

    def _target(self, request: PreparedRequest, content_type: str) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        ext = mimetypes.guess_extension(content_type.split(";", 1)[0].strip()) or ".bin"
        stem = request.operation_id.rsplit(".", 1)[-1]
        return self.output_dir / f"{stem}-{request.request_id or uuid.uuid4().hex}{ext}"

    async def write(
        self, request: PreparedRequest, content_type: str, chunks: AsyncIterator[bytes]
    ) -> BinaryRef:
        target = self._target(request, content_type)
        digest = hashlib.sha256()
        size = 0
        with target.open("wb") as fh:
            async for chunk in chunks:
                fh.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        return BinaryRef(
            path=str(target),
            bytes=size,
            contentType=content_type or "application/octet-stream",
            sha256=digest.hexdigest(),
        )


class RestTransport:
    """HTTP transport over httpx2: JSON, text/plain, multipart streamed from disk, binary to
    file, custom CA bundle, per-request id header."""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        verify: bool | str = True,
        request_id_header: str = "X-Request-Id",
        sink: BodySink | None = None,
        http_transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.request_id_header = request_id_header
        self.sink = sink or BodySink()
        # On-prem servers often use an internal CA: a path means "trust this bundle".
        tls: bool | ssl.SSLContext = (
            ssl.create_default_context(cafile=verify) if isinstance(verify, str) else verify
        )
        self._client = httpx2.AsyncClient(
            timeout=timeout,
            verify=tls,
            transport=http_transport,
            follow_redirects=False,
            trust_env=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _build(self, request: PreparedRequest, stack: ExitStack) -> httpx2.Request:
        headers = dict(request.headers)
        if request.request_id:
            headers[self.request_id_header] = request.request_id
        kwargs: dict[str, Any] = {"params": list(request.query), "headers": headers}
        if request.multipart:
            files: list[tuple[str, tuple[str | None, bytes | BinaryIO, str]]] = []
            for part in request.multipart:
                if isinstance(part, JsonPart):
                    files.append((part.name, (None, dumps(part.value), part.content_type)))
                else:
                    handle: BinaryIO = stack.enter_context(part.path.open("rb"))
                    files.append((part.name, (part.filename, handle, part.content_type)))
            kwargs["files"] = files
        elif request.text is not None:
            headers.setdefault("Content-Type", "text/plain; charset=utf-8")
            kwargs["content"] = request.text.encode("utf-8")
        elif request.json is not NO_BODY:
            headers.setdefault("Content-Type", "application/json")
            kwargs["content"] = dumps(request.json)
        return self._client.build_request(request.method, request.url, **kwargs)

    async def send(self, request: PreparedRequest) -> RawResponse:
        with ExitStack() as stack:
            try:
                built = self._build(request, stack)
                response = await self._client.send(built, stream=True)
            except httpx2.TimeoutException as exc:
                raise TransportFailure(f"timed out: {type(exc).__name__}", timeout=True) from None
            except httpx2.TransportError as exc:
                raise TransportFailure(f"{type(exc).__name__}: {exc}") from None
            try:
                headers = dict(response.headers)
                content_type = response.headers.get("content-type", "")
                if self.sink.wants_file(request, response.status_code, content_type):
                    ref = await self.sink.write(request, content_type, response.aiter_bytes())
                    return RawResponse(
                        response.status_code, headers, file=ref, request_id=request.request_id
                    )
                content = await response.aread()
                return RawResponse(
                    response.status_code, headers, content, request_id=request.request_id
                )
            except httpx2.TimeoutException as exc:
                raise TransportFailure(f"timed out: {type(exc).__name__}", timeout=True) from None
            except httpx2.TransportError as exc:
                raise TransportFailure(f"{type(exc).__name__}: {exc}") from None
            finally:
                await response.aclose()


# ---------------------------------------------------------------------------- mock


@dataclass
class MockReply:
    status: int = 200
    json: Any = NO_BODY
    body: bytes | str = b""
    headers: dict[str, str] = field(default_factory=dict)
    fail: TransportFailure | None = None


Handler = Callable[[PreparedRequest], "MockReply | Awaitable[MockReply]"]


class MockTransport:
    """Scripted transport for tests: records every ``PreparedRequest`` and replies from a queue
    per ``(METHOD, path)`` or from a handler. Nothing touches the network."""

    def __init__(self, handler: Handler | None = None, sink: BodySink | None = None) -> None:
        self.requests: list[PreparedRequest] = []
        self.handler = handler
        self.sink = sink or BodySink()
        self._routes: dict[tuple[str, str], deque[MockReply]] = {}
        self._sticky: dict[tuple[str, str], MockReply] = {}

    def add(self, method: str, path: str, *replies: MockReply, sticky: bool = False) -> None:
        """Queue replies for ``METHOD /path`` (path without the base URL, e.g. ``/api/pages``).
        With ``sticky`` the last reply repeats forever."""
        key = (method.upper(), path)
        self._routes.setdefault(key, deque()).extend(replies)
        if sticky and replies:
            self._sticky[key] = replies[-1]

    def calls(self, method: str | None = None, path: str | None = None) -> list[PreparedRequest]:
        return [
            r
            for r in self.requests
            if (method is None or r.method == method.upper())
            and (path is None or httpx2.URL(r.url).path.endswith(path))
        ]

    async def aclose(self) -> None:
        return None

    async def _reply(self, request: PreparedRequest) -> MockReply:
        path = httpx2.URL(request.url).path
        for (method, route), queue in self._routes.items():
            if method == request.method and path.endswith(route):
                if queue:
                    return queue.popleft()
                if (method, route) in self._sticky:
                    return self._sticky[(method, route)]
        if self.handler is not None:
            result = self.handler(request)
            return await result if asyncio.iscoroutine(result) else result  # type: ignore[return-value]
        raise AssertionError(f"MockTransport: no reply scripted for {request.method} {path}")

    async def send(self, request: PreparedRequest) -> RawResponse:
        self.requests.append(request)
        # Surface the same failure modes as a real transport would for missing upload files.
        for part in request.multipart:
            if isinstance(part, FilePart) and not part.path.is_file():
                raise TransportFailure(f"file vanished: {part.path.name}")
        reply = await self._reply(request)
        if reply.fail is not None:
            raise reply.fail
        headers = dict(reply.headers)
        if reply.json is not NO_BODY:
            content = dumps(reply.json)
            headers.setdefault("Content-Type", "application/json; charset=utf-8")
        else:
            content = reply.body.encode("utf-8") if isinstance(reply.body, str) else reply.body
        content_type = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
        if self.sink.wants_file(request, reply.status, content_type):

            async def one_chunk() -> AsyncIterator[bytes]:
                yield content

            ref = await self.sink.write(request, content_type, one_chunk())
            return RawResponse(reply.status, headers, file=ref, request_id=request.request_id)
        return RawResponse(reply.status, headers, content, request_id=request.request_id)


# ---------------------------------------------------------------------------- retries


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded exponential backoff for idempotent requests only (plan §4.1)."""

    max_retries: int = 2
    base_delay: float = 0.5
    max_delay: float = 4.0

    def delay(self, attempt: int) -> float:
        return float(min(self.max_delay, self.base_delay * (2**attempt)))


def _data_not_ready(request: PreparedRequest, response: RawResponse) -> bool:
    if response.status != 202:
        return False
    if request.may_be_not_ready:
        return True
    with contextlib.suppress(ValueError, AttributeError):
        body = json.loads(response.content or b"null")
        code = body.get("ErrorCode", body.get("Code"))
        return str(code) in {str(DATA_NOT_READY), "DataNotReady"}
    return False


def should_retry(request: PreparedRequest, response: RawResponse) -> bool:
    return request.idempotent and (
        response.status in RETRY_STATUSES or _data_not_ready(request, response)
    )


Sleep = Callable[[float], Awaitable[None]]


async def send_with_retries(
    transport: Transport,
    request: PreparedRequest,
    policy: RetryPolicy,
    sleep: Sleep = asyncio.sleep,
) -> RawResponse:
    """Send once; for GET/HEAD retry network errors, 503 and 202-DataNotReady with backoff.
    Writes are never retried: a failed write may still have happened on the server."""
    attempt = 0
    while True:
        try:
            response = await transport.send(request)
        except TransportFailure:
            if not request.idempotent or attempt >= policy.max_retries:
                raise
            logger.info("retrying %s after transport failure", request.operation_id)
        else:
            if attempt >= policy.max_retries or not should_retry(request, response):
                return replace(response, attempts=attempt + 1)
            logger.info("retrying %s after HTTP %s", request.operation_id, response.status)
        await sleep(policy.delay(attempt))
        attempt += 1


def unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))
