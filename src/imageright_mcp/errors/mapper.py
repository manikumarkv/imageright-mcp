"""ErrorMapper (plan §6.3): native REST / HTTP / SOAP failures -> IR error objects.

The mapper only classifies; transports (M4/M5) hand it the raw status, body or fault. Native
detail is always preserved under ``error.native``.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from imageright_mcp.errors.registry import Registry, get_registry

DATA_NOT_READY = 15
MAX_RAW = 2000

# Our object nouns per native-code family (see ERROR_FAMILIES in scripts/build_catalog.py).
FAMILY_KIND = {
    "notes": "note",
    "tasks": "task",
    "workflow": "workflow step",
    "documents": "document",
    "files": "file",
    "pages": "page",
    "drawers": "drawer",
    "object-types": "type",
    "folders": "folder",
    "marks": "mark",
    "users": "user",
    "groups-roles": "group or role",
    "sla": "SLA",
    "dashboard": "dashboard view",
    "ocr": "OCR form",
    "redaction": "redaction rule set",
    "config": "configuration node",
    "annotations": "annotation template",
}
# Nouns recognised in operation ids, most specific first.
OPERATION_NOUNS = (
    ("attribute", "attribute"),
    ("batch", "batch"),
    ("note", "note"),
    ("task", "task"),
    ("step", "workflow step"),
    ("workflow", "workflow"),
    ("document", "document"),
    ("page", "page"),
    ("folder", "folder"),
    ("file", "file"),
    ("drawer", "drawer"),
    ("user", "user"),
    ("group", "group"),
    ("role", "role"),
)


@dataclass(frozen=True)
class ErrorContext:
    """What the caller knows about the failed call; used for routing and hint templating."""

    operation_id: str | None = None
    object_kind: str | None = None
    # A 401 on a token that already worked means it expired (IR-2004); otherwise IR-2005.
    session_established: bool = False


def object_kind(operation_id: str | None, family: str | None = None) -> str | None:
    if family in FAMILY_KIND:
        return FAMILY_KIND[family]
    if not operation_id:
        return None
    name = operation_id.lower()
    if name.startswith("rest."):
        # rest.v1.<area>.<operation>: the area is the most reliable noun.
        parts = name.split(".")
        name = parts[2] if len(parts) > 2 else name
    for needle, kind in OPERATION_NOUNS:
        if needle in name:
            return kind
    return None


def _strip_stack(text: str) -> str:
    lines = [line.strip() for line in text.strip().splitlines()]
    return " ".join(line for line in lines if line and not line.startswith("at "))


def _clean_fault_message(fault_string: str) -> str:
    """Innermost exception message of a .NET fault string, without type prefix or stack."""
    inner = _strip_stack(fault_string.split("--->")[-1])
    return re.sub(r"^[\w.`]+(?:Exception|Fault)\s*:\s*", "", inner).strip()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


@dataclass(frozen=True)
class SoapFault:
    fault_code: str | None
    fault_string: str
    detail: str | None


def parse_soap_fault(xml: str | bytes) -> SoapFault | None:
    """Extract the Fault (SOAP 1.1 or 1.2) from a response; None when there is none.

    Raises ``xml.etree.ElementTree.ParseError`` for a body that is not XML.
    """
    root = ET.fromstring(xml)
    fault = next((el for el in root.iter() if _local(el.tag) == "Fault"), None)
    if fault is None:
        return None
    children = {_local(child.tag): child for child in fault}

    def text(el: ET.Element | None) -> str | None:
        if el is None:
            return None
        value = " ".join(t.strip() for t in el.itertext() if t.strip())
        return value or None

    if "Reason" in children:  # SOAP 1.2
        code_el = children.get("Code")
        values = (
            [] if code_el is None else [el for el in code_el.iter() if _local(el.tag) == "Value"]
        )
        value = values[0] if values else None
        return SoapFault(text(value), text(children["Reason"]) or "", text(children.get("Detail")))
    return SoapFault(
        text(children.get("faultcode")),
        text(children.get("faultstring")) or "",
        text(children.get("detail")),
    )


class ErrorMapper:
    def __init__(
        self,
        registry: Registry | None = None,
        operations: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self.registry = registry or get_registry()
        if operations is None:
            from imageright_mcp.catalog import get_catalog

            operations = get_catalog().ops
        self.operations = operations

    # ------------------------------------------------------------------ helpers

    def _template(self, context: ErrorContext, family: str | None = None) -> dict[str, str]:
        values: dict[str, str] = {}
        kind = context.object_kind or object_kind(context.operation_id, family)
        if kind:
            values["objectKind"] = kind
        if context.operation_id:
            values["operationId"] = context.operation_id
        return values

    def _raises_data_not_ready(self, operation_id: str | None) -> bool:
        op = self.operations.get(operation_id or "")
        return op is not None and DATA_NOT_READY in (op.get("errors") or [])

    def _native_code(self, body: Mapping[str, Any]) -> int | str | None:
        lowered = {str(k).lower(): v for k, v in body.items()}
        # The doc example uses ErrorCode, the OAS ErrorModel uses Code: accept both.
        raw = lowered.get("errorcode", lowered.get("code"))
        if isinstance(raw, bool) or raw is None:
            return None
        if isinstance(raw, int):
            return raw
        text = str(raw).strip()
        if re.fullmatch(r"-?\d+", text):
            return int(text)
        # ErrorCodes is a string enum in the OAS, so the code may arrive as its name.
        return text or None

    # ------------------------------------------------------------------ REST

    def from_rest(
        self,
        status: int,
        body: Any,
        context: ErrorContext | None = None,
        surface: str = "rest-v1",
    ) -> dict[str, Any] | None:
        """Classify a REST response; None when it is a success."""
        context = context or ErrorContext()
        if 200 <= status < 300 and status != 202:
            # A success body is the result, never an error model: a created object may well
            # carry its own "code" field. Only 202 can carry DataNotReady.
            return None
        if isinstance(body, bytes | str):
            try:
                body = json.loads(body) if body else None
            except ValueError:
                body = None
        mapping = body if isinstance(body, Mapping) else {}
        native_code = self._native_code(mapping)
        message = next((v for k, v in mapping.items() if str(k).lower() == "message" and v), None)
        if isinstance(native_code, str):
            resolved = self.registry.native_code_for_name(native_code)
            native_name: str | None = native_code
            native_code = resolved
        else:
            native_name = None
        native: dict[str, Any] = {
            "surface": surface,
            "httpStatus": status,
            "code": native_code,
            "name": native_name,
            "message": message,
        }
        if native_code is not None:
            info = self.registry.native_info(native_code)
            native["name"] = info["name"] if info else native_name
            return self.registry.error(
                self.registry.for_native_rest(native_code),
                context=self._template(context, info["family"] if info else None),
                native=native,
            )
        if native_name is not None:  # a name we have never seen
            return self.registry.error("IR-5099", context=self._template(context), native=native)
        fallback = self._http_fallback(status, context)
        if fallback is None:
            return None
        return self.registry.error(fallback, context=self._template(context), native=native)

    def _http_fallback(self, status: int, context: ErrorContext) -> str | None:
        if status == 202:
            return "IR-5006" if self._raises_data_not_ready(context.operation_id) else None
        if 200 <= status < 300:
            return None
        if status == 401:
            return "IR-2004" if context.session_established else "IR-2005"
        candidates = self.registry.for_http(status)
        if candidates:
            return candidates[0]
        if status >= 500:
            return "IR-5001"
        if status >= 400:
            return "IR-4101"
        return "IR-5099"

    # ------------------------------------------------------------------ transport

    def from_transport_error(
        self, exc: BaseException, context: ErrorContext | None = None
    ) -> dict[str, Any]:
        context = context or ErrorContext()
        code = "IR-5005" if isinstance(exc, TimeoutError) else "IR-5004"
        native = {"surface": None, "exception": type(exc).__name__, "message": str(exc)}
        return self.registry.error(code, context=self._template(context), native=native)

    # ------------------------------------------------------------------ SOAP

    def from_soap_fault(
        self, xml: str | bytes, context: ErrorContext | None = None
    ) -> dict[str, Any] | None:
        """Classify a SOAP response body; None when it carries no Fault."""
        context = context or ErrorContext()
        try:
            fault = parse_soap_fault(xml)
        except ET.ParseError as exc:
            raw = xml.decode("utf-8", "replace") if isinstance(xml, bytes) else xml
            unparsed = {"surface": "soap", "parseError": str(exc), "raw": raw[:MAX_RAW]}
            return self.registry.error("IR-9002", context=self._template(context), native=unparsed)
        if fault is None:
            return None
        message = _clean_fault_message(fault.fault_string)
        native: dict[str, Any] = {
            "surface": "soap",
            "faultCode": fault.fault_code,
            "faultString": fault.fault_string,
            "message": message,
            "detail": fault.detail,
        }
        match = self.registry.match_fault(message) or self.registry.match_fault(
            " ".join(filter(None, [_strip_stack(fault.fault_string), fault.detail]))
        )
        if match is None:
            return self.registry.error("IR-5003", context=self._template(context), native=native)
        native["matchedPattern"] = match[1]
        return self.registry.error(match[0], context=self._template(context), native=native)

    def from_soap_result(
        self, operation_id: str, result: Any, context: ErrorContext | None = None
    ) -> dict[str, Any] | None:
        """Result-level failures the catalog marks (plan §6.3), e.g. FindUserByName -> null."""
        context = context or ErrorContext(operation_id=operation_id)
        rule = self.registry.result_failures.get(operation_id)
        if rule is None:
            return None
        when = rule["when"]
        message: str | None = None
        if when == "null":
            failed = result is None
        elif when == "false":
            failed = result is False
        else:  # falseField
            failed = isinstance(result, Mapping) and result.get(rule["field"]) is False
            if failed and isinstance(result, Mapping):
                raw = result.get(rule.get("messageField", ""))
                message = str(raw) if raw else None
        if not failed:
            return None
        native = {
            "surface": "soap",
            "operation": operation_id.removeprefix("soap."),
            "result": result,
            "message": message,
        }
        match = self.registry.match_fault(message) if message else None
        if match:
            native["matchedPattern"] = match[1]
        code = match[0] if match else rule["code"]
        return self.registry.error(code, context=self._template(context), native=native)
