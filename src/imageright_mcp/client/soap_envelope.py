"""SOAP 1.1 envelopes for ``irwebservice40.asmx`` (plan §4.1), driven by the generated operation
table (``data/catalog/soap_table.json``).

Requests are document/literal in the ASMX style: the operation element carries the service
namespace as its default namespace and each argument is a child element in table order. Complex
values are serialized from dicts in XSD sequence order (base type first), whatever order the
caller's dict has. The ``ref securityToken`` is left as a slot and filled at send time, so a
preview shows ``<securityToken>***</securityToken>`` and hashes the same as the real request.

Responses become JSON with the PascalCase names the service uses: ``ArrayOfX`` collapses to a
list (``[]`` when empty, never null), int64 stays a Python int, and binary results go to a file.
"""

from __future__ import annotations

import base64
import binascii
import re
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from imageright_mcp.client.builder import BuildError
from imageright_mcp.client.models import PreparedRequest
from imageright_mcp.config import REDACTED
from imageright_mcp.errors import get_registry

SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
XSD = "http://www.w3.org/2001/XMLSchema"
CONTENT_TYPE = "text/xml; charset=utf-8"
PLACEHOLDER_URL = "https://{soapUrl}"
TOKEN_ELEMENT = "securityToken"
INDENT = "  "
# Nested base64 values up to this size stay inline (keys, thumbnails); larger ones go to a file.
INLINE_BINARY_LIMIT = 1024
MAX_DEPTH = 16

_INVALID_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
_XSI_NIL = f"{{{XSI}}}nil"
_XSI_TYPE = f"{{{XSI}}}type"
_INTEGERS = frozenset({"long", "int", "short", "unsignedByte", "byte"})

BinaryWriter = Callable[[bytes, str], dict[str, Any]]


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def escape(value: str, where: str) -> str:
    """XML text content. Characters XML 1.0 cannot carry are rejected, not silently dropped."""
    if _INVALID_XML.search(value):
        raise BuildError(
            get_registry().error(
                "IR-3006", message=f"{where} contains a control character XML cannot carry."
            )
        )
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\r", "&#xD;")
    )


class SoapTable:
    """The generated operation table: ops by name and by operationId, types by name."""

    def __init__(self, raw: Mapping[str, Any]) -> None:
        self.namespace: str = raw["namespace"]
        self.operations: dict[str, dict[str, Any]] = raw["operations"]
        self.types: dict[str, dict[str, Any]] = raw["types"]
        self._by_id = {str(op["operationId"]): op for op in self.operations.values()}

    def op(self, operation_id: str) -> dict[str, Any]:
        entry: dict[str, Any] = self._by_id[operation_id]
        return entry

    def kind(self, type_name: str) -> str:
        record = self.types.get(type_name)
        if record is not None:
            return str(record["kind"])
        return "any" if type_name == "inline" else "primitive"


@dataclass(frozen=True)
class SoapCall:
    """One resolved SOAP request. The envelope is complete except for the token value."""

    operation: str
    operation_id: str
    soap_action: str
    url: str
    response_element: str
    result: Mapping[str, Any] | None
    token_arg: str | None
    echoes_token: bool
    # Replays after re-login and network retries are only allowed for reads (plan §4.4).
    idempotent: bool
    head: str
    tail: str = ""

    def envelope(self, token: str | None) -> str:
        if self.token_arg is None:
            return self.head
        return self.head + escape(token or "", self.token_arg) + self.tail

    def prepared(self, token: str | None, request_id: str | None = None) -> PreparedRequest:
        return PreparedRequest(
            method="POST",
            url=self.url,
            operation_id=self.operation_id,
            headers={"SOAPAction": f'"{self.soap_action}"', "Content-Type": CONTENT_TYPE},
            text=self.envelope(token),
            idempotent=self.idempotent,
            request_id=request_id or uuid.uuid4().hex,
        )

    def preview(self) -> PreparedRequest:
        """What dry-run shows and hashes: the token slot reads ``***``."""
        return self.prepared(REDACTED, request_id="preview")


# ---------------------------------------------------------------------------- requests


class EnvelopeBuilder:
    def __init__(self, table: SoapTable) -> None:
        self.table = table

    def build(self, op: Mapping[str, Any], params: Mapping[str, Any], url: str | None) -> SoapCall:
        entry = self.table.op(str(op["id"]))
        name = str(entry["requestElement"])
        head = [
            '<?xml version="1.0" encoding="utf-8"?>',
            f'<soap:Envelope xmlns:soap="{SOAP_ENV}" xmlns:xsi="{XSI}" xmlns:xsd="{XSD}">',
            f"{INDENT}<soap:Body>",
            f'{INDENT * 2}<{name} xmlns="{self.table.namespace}">',
        ]
        tail: list[str] = []
        token_arg: str | None = None
        lines = head
        for arg in sorted(entry["args"], key=lambda a: int(a["order"])):
            arg_name = str(arg["name"])
            if arg["token"]:
                token_arg = arg_name
                # Split here: the token value is spliced in at send time.
                head.append(f"{INDENT * 3}<{arg_name}>")
                lines = tail
                tail.append(f"</{arg_name}>")
                continue
            if arg_name not in params:
                continue
            self._write(
                lines,
                arg_name,
                str(arg["type"]),
                str(arg["kind"]),
                params[arg_name],
                3,
                nullable=False,
                repeated=bool(arg.get("repeated")),
            )
        closing = [f"{INDENT * 2}</{name}>", f"{INDENT}</soap:Body>", "</soap:Envelope>", ""]
        lines.extend(closing)
        if token_arg is None:
            return self._call(op, entry, url, None, "\n".join(head), "")
        # head ends with "<securityToken>" (no newline after it); tail starts with its close tag.
        head_text = "\n".join(head)
        tail_text = tail[0] + "\n" + "\n".join(tail[1:])
        return self._call(op, entry, url, token_arg, head_text, tail_text)

    def _call(
        self,
        op: Mapping[str, Any],
        entry: Mapping[str, Any],
        url: str | None,
        token_arg: str | None,
        head: str,
        tail: str,
    ) -> SoapCall:
        return SoapCall(
            operation=str(op["operation"]),
            operation_id=str(op["id"]),
            soap_action=str(entry["soapAction"]),
            url=url or PLACEHOLDER_URL,
            response_element=str(entry["responseElement"]),
            result=entry["result"],
            token_arg=token_arg,
            echoes_token=bool(entry["echoesToken"]),
            idempotent=entry["safety"] == "read",
            head=head,
            tail=tail,
        )

    def _write(
        self,
        out: list[str],
        name: str,
        type_name: str,
        kind: str,
        value: Any,
        depth: int,
        *,
        nullable: bool,
        repeated: bool = False,
    ) -> None:
        if repeated and isinstance(value, list | tuple):
            for item in value:
                self._write(out, name, type_name, kind, item, depth, nullable=nullable)
            return
        pad = INDENT * depth
        if value is None:
            if nullable:
                out.append(f'{pad}<{name} xsi:nil="true" />')
            return
        if depth > MAX_DEPTH:
            raise BuildError(get_registry().error("IR-3006", message=f"{name} is nested too deep."))
        if kind == "array":
            item = self.table.types[type_name]["item"]
            items = _array_items(value, str(item["name"]))
            children: list[str] = []
            for element in items:
                self._write(
                    children,
                    str(item["name"]),
                    str(item["type"]),
                    str(item["kind"]),
                    element,
                    depth + 1,
                    nullable=bool(item.get("nullable")),
                )
            _wrap(out, pad, name, children)
        elif kind == "complex" and isinstance(value, Mapping):
            children = []
            for spec in self.table.types[type_name]["fields"]:
                field_name = str(spec["name"])
                if field_name not in value:
                    continue
                self._write(
                    children,
                    field_name,
                    str(spec["type"]),
                    str(spec["kind"]),
                    value[field_name],
                    depth + 1,
                    nullable=bool(spec.get("nullable")),
                    repeated=bool(spec.get("repeated")),
                )
            _wrap(out, pad, name, children)
        elif kind == "any":
            xsi_type, text = _any_value(value)
            out.append(f'{pad}<{name} xsi:type="{xsi_type}">{escape(text, name)}</{name}>')
        else:
            text = escape(_scalar(type_name, kind, value), name)
            out.append(f"{pad}<{name}>{text}</{name}>" if text else f"{pad}<{name} />")


def _wrap(out: list[str], pad: str, name: str, children: list[str]) -> None:
    if children:
        out.append(f"{pad}<{name}>")
        out.extend(children)
        out.append(f"{pad}</{name}>")
    else:
        out.append(f"{pad}<{name} />")


def _array_items(value: Any, item_name: str) -> list[Any]:
    """``ArrayOfX`` accepts a plain list or the wire shape ``{"X": [...]}``."""
    if isinstance(value, Mapping):
        value = value.get(item_name, [])
    if isinstance(value, list | tuple):
        return list(value)
    return [value]


def _scalar(type_name: str, kind: str, value: Any) -> str:
    """Text for a primitive, enum or flags value. The validator has already checked types;
    anything else is rendered as text so a failed validation still gets a readable preview."""
    if kind == "flags" and isinstance(value, list | tuple):
        return " ".join(str(v) for v in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if type_name == "char" and isinstance(value, str) and len(value) == 1:
        return str(ord(value))  # .NET serializes System.Char as its UTF-16 code
    if type_name == "base64Binary" and isinstance(value, bytes | bytearray):
        return base64.b64encode(bytes(value)).decode("ascii")
    return str(value)


def _any_value(value: Any) -> tuple[str, str]:
    """An untyped (xs:anyType) element needs xsi:type, or .NET reads it as an XmlNode[]."""
    if isinstance(value, bool):
        return "xsd:boolean", "true" if value else "false"
    if isinstance(value, int):
        return ("xsd:int" if -(2**31) <= value < 2**31 else "xsd:long"), str(value)
    if isinstance(value, float):
        return "xsd:double", repr(value)
    return "xsd:string", str(value)


# ---------------------------------------------------------------------------- responses


@dataclass
class ParsedResponse:
    fault: bool
    token: str | None
    result: Any = None
    shape: str | None = None
    # The body was XML but not the expected response element (or a Fault).
    unexpected: str | None = None


def find_token(root: ET.Element, response: ET.Element | None) -> str | None:
    """The echoed ``securityToken``: in the response element, else anywhere (a fault's detail
    or a header), so a fault that still carries a token rotates it too."""
    candidates: list[ET.Element] = []
    if response is not None:
        candidates = [c for c in response if local(c.tag) == TOKEN_ELEMENT]
    if not candidates:
        candidates = [el for el in root.iter() if local(el.tag) == TOKEN_ELEMENT]
    for element in candidates:
        text = (element.text or "").strip()
        if text:
            return text
    return None


class ResponseParser:
    def __init__(self, table: SoapTable, write_binary: BinaryWriter) -> None:
        self.table = table
        self.write_binary = write_binary

    def parse(self, call: SoapCall, content: bytes) -> ParsedResponse:
        """Raises ``xml.etree.ElementTree.ParseError`` for a body that is not XML."""
        root = ET.fromstring(content)
        body = next((el for el in root if local(el.tag) == "Body"), None)
        first = next(iter(body), None) if body is not None else None
        if first is None:
            return ParsedResponse(False, find_token(root, None), unexpected="empty SOAP body")
        if local(first.tag) == "Fault":
            return ParsedResponse(True, find_token(root, None))
        token = find_token(root, first)
        if local(first.tag) != call.response_element:
            return ParsedResponse(False, token, unexpected=f"unexpected element {local(first.tag)}")
        spec = call.result
        if spec is None:
            return ParsedResponse(False, token)
        element = next((c for c in first if local(c.tag) == spec["name"]), None)
        type_name, kind = str(spec["type"]), str(spec["kind"])
        shape = f"soap:{type_name}"
        if element is None:
            return ParsedResponse(False, token, [] if kind == "array" else None, shape)
        if type_name == "base64Binary" and not _is_nil(element):
            raw = _b64(element.text)
            if raw is not None:
                return ParsedResponse(False, token, self.write_binary(raw, call.operation), shape)
        value = self._convert(element, type_name, kind, call.operation, 0)
        return ParsedResponse(False, token, value, shape)

    def _convert(self, el: ET.Element, type_name: str, kind: str, stem: str, depth: int) -> Any:
        if _is_nil(el):
            return None
        declared = el.get(_XSI_TYPE)
        if declared:
            derived = declared.rsplit(":", 1)[-1]
            if derived in self.table.types:
                type_name, kind = derived, self.table.kind(derived)
            elif kind == "any":
                type_name, kind = derived, "primitive"
        if depth > MAX_DEPTH:
            return _generic(el)
        if kind == "array":
            item = self.table.types[type_name]["item"]
            return [
                self._convert(c, str(item["type"]), str(item["kind"]), stem, depth + 1)
                for c in el
                if local(c.tag) == item["name"]
            ]
        if kind == "complex":
            return self._object(el, self.table.types[type_name]["fields"], stem, depth)
        if kind == "flags":
            return (el.text or "").split()
        if kind == "any":
            return _generic(el)
        if kind == "enum":
            return el.text or ""
        return self._primitive(type_name, el.text or "", stem)

    def _object(
        self, el: ET.Element, fields: list[dict[str, Any]], stem: str, depth: int
    ) -> dict[str, Any]:
        by_name = {str(f["name"]): f for f in fields}
        out: dict[str, Any] = {}
        for child in el:
            name = local(child.tag)
            spec = by_name.get(name)
            if spec is None:  # a derived type's or a newer server's field: keep it
                value = _generic(child)
                if name in out:
                    existing = out[name]
                    out[name] = (
                        [*existing, value] if isinstance(existing, list) else [existing, value]
                    )
                else:
                    out[name] = value
                continue
            value = self._convert(child, str(spec["type"]), str(spec["kind"]), stem, depth + 1)
            if spec.get("repeated"):
                out.setdefault(name, []).append(value)
            else:
                out[name] = value
        for name, spec in by_name.items():
            if name not in out and (spec.get("repeated") or spec["kind"] == "array"):
                out[name] = []
        return out

    def _primitive(self, type_name: str, text: str, stem: str) -> Any:
        try:
            if type_name in _INTEGERS:
                return int(text)
            if type_name == "boolean":
                return text.strip() in {"true", "1"}
            if type_name in {"double", "float"}:
                return float(text)
            if type_name == "char":
                return chr(int(text))
        except ValueError:
            return text
        if type_name == "base64Binary":
            raw = _b64(text)
            if raw is not None and len(raw) > INLINE_BINARY_LIMIT:
                return self.write_binary(raw, stem)
        # decimal stays text so no precision is lost; dateTime is already ISO-8601.
        return text


def _is_nil(el: ET.Element) -> bool:
    return el.get(_XSI_NIL) in {"true", "1"}


def _b64(text: str | None) -> bytes | None:
    try:
        return base64.b64decode((text or "").strip(), validate=True)
    except (binascii.Error, ValueError):
        return None


def _generic(el: ET.Element) -> Any:
    """Schema-less conversion: children become a dict (repeated names a list), leaves text."""
    if _is_nil(el):
        return None
    children = list(el)
    if not children:
        return el.text or ""
    out: dict[str, Any] = {}
    for child in children:
        name = local(child.tag)
        value = _generic(child)
        if name in out:
            existing = out[name]
            out[name] = [*existing, value] if isinstance(existing, list) else [existing, value]
        else:
            out[name] = value
    return out
