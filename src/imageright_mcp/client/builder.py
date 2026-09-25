"""REST request builder (plan §4.1, pipeline step 5): catalog operation + params -> PreparedRequest.

Paths are taken verbatim from the catalog (v1 ``/api/...``, v2 ``/api/v2/...``) and appended to
``restBaseUrl``. Multipart part names and their order come from the catalog as well. Credentials
are not attached here (that is the AuthManager's job), so dry-run needs none.
"""

from __future__ import annotations

import mimetypes
import re
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

from imageright_mcp.client.models import (
    NO_BODY,
    FilePart,
    JsonPart,
    MultipartPart,
    PreparedRequest,
    file_digest,
    is_idempotent,
)
from imageright_mcp.client.validator import json_part_name, numbered_sibling
from imageright_mcp.errors import get_registry

PLACEHOLDER_BASE = "https://{restBaseUrl}"
DATA_NOT_READY = 15
SCALAR_TYPES = frozenset({"string", "int64", "int32", "boolean", "date-time", "date", "guid"})


class BuildError(Exception):
    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(error["message"])
        self.error = error


def _query_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _success_types(op: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [r for status, r in (op.get("responses") or {}).items() if str(status).startswith("2")]


class RequestBuilder:
    def __init__(self, base_url: str | None, file_roots: list[str] | None = None) -> None:
        self.base_url = base_url.rstrip("/") if base_url else None
        roots = file_roots or [str(Path.cwd())]
        self.file_roots = [Path(r).expanduser().resolve() for r in roots]

    def build(
        self,
        op: Mapping[str, Any],
        params: Mapping[str, Any],
        files: Mapping[str, str],
    ) -> PreparedRequest:
        op_id = str(op["id"])
        method = str(op["method"]).upper()
        declared = {str(p["name"]): p for p in op["params"]}
        path = str(op["path"])
        for name, spec in declared.items():
            if spec["in"] == "path" and params.get(name) is not None:
                path = path.replace("{" + name + "}", quote(str(params[name]), safe=""))
        query: list[tuple[str, str]] = []
        for name, spec in declared.items():
            if spec["in"] != "query" or params.get(name) is None:
                continue
            value = params[name]
            for item in value if isinstance(value, list) else [value]:
                query.append((name, _query_value(item)))

        body: Any = NO_BODY
        text: str | None = None
        multipart: tuple[MultipartPart, ...] = ()
        request_body = op.get("requestBody") or {}
        content_type = request_body.get("contentType")
        body_params = {n: s for n, s in declared.items() if s["in"] == "body"}
        whole = next((n for n, s in body_params.items() if s.get("wholeBody")), None)
        if content_type == "multipart/form-data":
            multipart = self._multipart(op, params, files, body_params)
        elif whole is not None:
            value = params.get(whole)
            if content_type == "text/plain":
                text = None if value is None else str(value)
            elif value is not None:
                body = value
        else:
            fields = {n: params[n] for n in body_params if n in params}
            if fields or request_body.get("required"):
                body = fields
        if text is None and body is NO_BODY and not multipart and content_type == "text/plain":
            text = ""

        success = _success_types(op)
        return PreparedRequest(
            method=method,
            url=(self.base_url or PLACEHOLDER_BASE) + path,
            operation_id=op_id,
            query=tuple(query),
            json=body,
            text=text,
            multipart=multipart,
            idempotent=is_idempotent(method),
            may_be_not_ready=DATA_NOT_READY in (op.get("errors") or []),
            expects_binary=any(r.get("type") in {"binary", "file"} for r in success),
            expects_scalar=bool(success)
            and all(r.get("type") in SCALAR_TYPES for r in success if r.get("type")),
            request_id=uuid.uuid4().hex,
        )

    def _multipart(
        self,
        op: Mapping[str, Any],
        params: Mapping[str, Any],
        files: Mapping[str, str],
        body_params: Mapping[str, Any],
    ) -> tuple[MultipartPart, ...]:
        json_part = json_part_name(op)
        parts: list[MultipartPart] = []
        declared = {str(p["name"]) for p in op["params"]}
        # image1, image2, ... follow the catalog's image0 in numeric order.
        extras = sorted(
            (n for n in files if n not in declared and numbered_sibling(op, n) is not None),
            key=lambda n: int(re.sub(r"\D", "", n) or 0),
        )
        for spec in op["params"]:
            if spec["in"] != "multipart":
                continue
            name = str(spec["name"])
            if name == json_part:
                value = params.get(name)
                if value is None:
                    value = {n: params[n] for n in body_params if n in params}
                parts.append(JsonPart(name, value))
            elif spec["type"] == "file" and name in files:
                parts.append(self._file(name, files[name]))
                parts.extend(
                    self._file(extra, files[extra])
                    for extra in extras
                    if numbered_sibling(op, extra) == name
                )
        return tuple(parts)

    def file_part(self, name: str, raw_path: str) -> FilePart:
        """Resolve a local upload inside the allowed roots (IR-1007 outside, IR-3006 missing)."""
        return self._file(name, raw_path)

    def _file(self, name: str, raw_path: str) -> FilePart:
        registry = get_registry()
        path = Path(raw_path).expanduser()
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise BuildError(
                registry.error("IR-3006", message=f"File for part {name} does not exist.")
            ) from None
        if not any(resolved.is_relative_to(root) for root in self.file_roots):
            raise BuildError(
                registry.error(
                    "IR-1007",
                    message=f"The file for part {name} is outside the allowed roots.",
                    native={"allowedRoots": [str(r) for r in self.file_roots]},
                )
            )
        if not resolved.is_file():
            raise BuildError(
                registry.error("IR-3006", message=f"The path for part {name} is not a file.")
            )
        size, sha = file_digest(resolved)
        guessed = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
        return FilePart(name=name, path=resolved, size=size, sha256=sha, content_type=guessed)
