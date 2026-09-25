"""Catalog-driven request validation (plan §4.2 step 4, §7.2): required params, types, enums,
and per-version availability of operations, params, schema fields and enum values.

Everything comes from the generated catalog; nothing here knows about a particular endpoint.
Issues map to IR-3005 (missing), IR-3006 (bad value), IR-3007 (not in this version) and
IR-3002 (operation absent from the profile).
"""

from __future__ import annotations

import base64
import binascii
import difflib
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from imageright_mcp.client.models import FileBase64

INT_RANGES = {
    "int16": (-(2**15), 2**15 - 1),
    "int32": (-(2**31), 2**31 - 1),
    "int64": (-(2**63), 2**63 - 1),
}
STRINGISH = {"string", "binary", "byte"}
ANY = {"any", "object", "application/json"}
# XSD built-ins on the SOAP surface, in the vocabulary the REST checks already use.
SOAP_TYPES = {
    "long": "int64",
    "int": "int32",
    "short": "int16",
    "dateTime": "date-time",
    "inline": "any",  # an untyped element (xs:anyType); the envelope adds xsi:type
}
MAX_DEPTH = 8
_MAP = re.compile(r"^map<string,(.+)>$")
_DECIMAL = re.compile(r"-?\d+(\.\d+)?")


@dataclass(frozen=True)
class Issue:
    code: str
    param: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "param": self.param, "message": self.message}


@dataclass
class Validation:
    issues: list[Issue] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "issues": [i.to_dict() for i in self.issues]}


def multipart_params(op: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [p for p in op["params"] if p["in"] == "multipart"]


def json_part_name(op: Mapping[str, Any]) -> str | None:
    """The multipart part that carries the JSON settings (e.g. PageCreateData), per catalog."""
    for p in multipart_params(op):
        if p["type"] == "application/json":
            return str(p["name"])
    return None


def file_part_names(op: Mapping[str, Any]) -> list[str]:
    return [str(p["name"]) for p in multipart_params(op) if p["type"] == "file"]


def numbered_sibling(op: Mapping[str, Any], name: str) -> str | None:
    """``image3`` is allowed when the catalog lists ``image0`` ("add image1, image2 ...")."""
    match = re.fullmatch(r"(.*?)(\d+)", name)
    if match is None:
        return None
    for known in file_part_names(op):
        known_match = re.fullmatch(r"(.*?)(\d+)", known)
        if known_match and known_match.group(1) == match.group(1):
            return known
    return None


class Validator:
    def __init__(self, schemas: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> None:
        self.schemas = schemas

    # ------------------------------------------------------------------ entry point

    def validate(
        self,
        op: Mapping[str, Any],
        profile: str,
        params: Mapping[str, Any],
        files: Mapping[str, str],
    ) -> Validation:
        result = Validation()
        op_id = str(op["id"])
        availability = op["availability"].get(profile, "absent")
        if availability == "absent":
            present = sorted(p for p, v in op["availability"].items() if v != "absent")
            result.issues.append(
                Issue(
                    "IR-3002",
                    op_id,
                    f"{op_id} does not exist in {profile}; it is available in "
                    f"{', '.join(present) or 'no profile'}.",
                )
            )
        deprecation = op.get("deprecation")
        if deprecation and profile in deprecation.get("in", []):
            result.warnings.append(
                {
                    "code": "IR-3003",
                    "message": f"{op_id} is deprecated; use {deprecation.get('replacement')}.",
                }
            )

        declared = {str(p["name"]): p for p in op["params"]}
        # The SOAP securityToken belongs to the session manager, never to the caller.
        tokens = {n for n, p in declared.items() if p.get("token")}
        declared = {n: p for n, p in declared.items() if n not in tokens}
        json_part = json_part_name(op)
        surface = str(op["surface"])
        param_availability: Mapping[str, list[str]] = op.get("paramAvailability") or {}

        for name in params:
            if name in tokens:
                result.issues.append(
                    Issue(
                        "IR-3006",
                        name,
                        f"{name} is managed by the server's SOAP session; do not pass it.",
                    )
                )
            elif name in declared and declared[name]["type"] == "file":
                result.issues.append(
                    Issue("IR-3006", name, f"{name} is a file part; pass it in files, not params.")
                )
            elif name not in declared:
                result.issues.append(self._unknown(name, list(declared), "parameter"))
        for name in files:
            if name not in declared and numbered_sibling(op, name) is None:
                result.issues.append(self._unknown(name, file_part_names(op), "file part"))
            elif name in declared and declared[name]["type"] != "file":
                result.issues.append(
                    Issue("IR-3006", name, f"{name} is not a file part; pass it in params.")
                )

        # Body params of a multipart op live inside the JSON part; accept either form.
        body_source: Mapping[str, Any] = params
        if json_part and json_part in params:
            given = params[json_part]
            if not isinstance(given, Mapping):
                result.issues.append(
                    Issue("IR-3006", json_part, f"{json_part} must be a JSON object.")
                )
                given = {}
            body_source = given
            stray = [n for n in params if n in declared and declared[n]["in"] == "body"]
            for name in stray:
                result.issues.append(
                    Issue(
                        "IR-3006",
                        name,
                        f"{name} is given both on its own and inside {json_part}; use one.",
                    )
                )

        for name, spec in declared.items():
            where = spec["in"]
            source = body_source if where == "body" else params
            if where == "multipart":
                if spec["type"] == "file" and spec.get("required") and name not in files:
                    result.issues.append(Issue("IR-3005", name, f"File part {name} is required."))
                continue
            value = source.get(name)
            label = f"{json_part}.{name}" if where == "body" and body_source is not params else name
            if name in source and name in param_availability:
                allowed = param_availability[name]
                if profile not in allowed:
                    result.issues.append(
                        Issue(
                            "IR-3007",
                            label,
                            f"{name} exists only in {', '.join(allowed)}, not {profile}.",
                        )
                    )
                    continue
            if value is None:
                if name in source and spec.get("nullable"):
                    continue
                if spec.get("required") or where == "path":
                    result.issues.append(Issue("IR-3005", label, f"{name} is required."))
                continue
            result.issues.extend(
                self._check(value, str(spec["type"]), surface, profile, label, where, 0)
            )
        return result

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _unknown(name: str, known: list[str], kind: str) -> Issue:
        lowered = {k.lower(): k for k in known}
        close = [lowered[name.lower()]] if name.lower() in lowered else []
        close = close or difflib.get_close_matches(name, known, n=3, cutoff=0.6)
        hint = f" Did you mean {', '.join(close)}?" if close else ""
        return Issue("IR-3006", name, f"Unknown {kind} {name!r}.{hint}")

    def _schema(self, surface: str, name: str) -> Mapping[str, Any] | None:
        return self.schemas.get(surface, {}).get(name)

    def _fields(self, surface: str, schema: Mapping[str, Any], depth: int = 0) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        for parent in schema.get("extends") or []:
            parent_schema = self._schema(surface, parent)
            if parent_schema and depth < MAX_DEPTH:
                fields.update(self._fields(surface, parent_schema, depth + 1))
        fields.update(schema.get("fields") or {})
        return fields

    def _check(
        self,
        value: Any,
        type_name: str,
        surface: str,
        profile: str,
        label: str,
        where: str,
        depth: int,
    ) -> list[Issue]:
        if surface == "soap":
            type_name = SOAP_TYPES.get(type_name, type_name)
        if depth > MAX_DEPTH or type_name in ANY:
            return []
        bad = [Issue("IR-3006", label, f"{label} must be {type_name}, got {_describe(value)}.")]
        if type_name.endswith("[]"):
            if not isinstance(value, list):
                return bad
            issues: list[Issue] = []
            for i, item in enumerate(value):
                issues += self._check(
                    item, type_name[:-2], surface, profile, f"{label}[{i}]", where, depth + 1
                )
            return issues
        mapped = _MAP.match(type_name)
        if mapped:
            if not isinstance(value, Mapping) or not all(isinstance(k, str) for k in value):
                return bad
            issues = []
            for key, item in value.items():
                issues += self._check(
                    item, mapped.group(1), surface, profile, f"{label}.{key}", where, depth + 1
                )
            return issues
        if type_name in INT_RANGES:
            number = value
            # Path and query values travel as text anyway; accept digit strings there.
            if (
                where in {"path", "query"}
                and isinstance(value, str)
                and re.fullmatch(r"-?\d+", value)
            ):
                number = int(value)
            if isinstance(number, bool) or not isinstance(number, int):
                return bad
            low, high = INT_RANGES[type_name]
            if not low <= number <= high:
                return [Issue("IR-3006", label, f"{label} is outside the {type_name} range.")]
            return []
        if type_name == "boolean":
            return [] if isinstance(value, bool) else bad
        if type_name == "decimal" and isinstance(value, str):
            return [] if _DECIMAL.fullmatch(value) else bad  # text keeps full precision
        if type_name in {"number", "double", "float", "decimal"}:
            return [] if isinstance(value, int | float) and not isinstance(value, bool) else bad
        if type_name == "char":
            return [] if isinstance(value, str) and len(value) == 1 else bad
        if type_name == "base64Binary":
            if isinstance(value, FileBase64):
                return []
            if not isinstance(value, str):
                return bad
            try:
                base64.b64decode(value, validate=True)
            except (binascii.Error, ValueError):
                return [Issue("IR-3006", label, f"{label} must be base64 text.")]
            return []
        if type_name in STRINGISH:
            return [] if isinstance(value, str) else bad
        if type_name in {"date-time", "date"}:
            if not isinstance(value, str):
                return bad
            try:
                if type_name == "date":
                    date.fromisoformat(value)
                else:
                    datetime.fromisoformat(value)
            except ValueError:
                return [Issue("IR-3006", label, f"{label} must be an ISO-8601 {type_name}.")]
            return []
        if type_name == "guid":
            try:
                uuid.UUID(str(value))
            except ValueError:
                return [Issue("IR-3006", label, f"{label} must be a GUID.")]
            return []
        schema = self._schema(surface, type_name)
        if schema is None:
            return []  # a type the catalog does not describe: let the server judge
        if schema.get("kind") == "enum":
            return self._check_enum(value, type_name, schema, profile, label)
        if schema.get("kind") == "flags":
            items = value.split() if isinstance(value, str) else value
            if not isinstance(items, list):
                return bad
            issues = []
            for item in items:
                issues += self._check_enum(item, type_name, schema, profile, label)
            return issues
        item = _array_item(surface, type_name, schema)
        if item is not None and isinstance(value, list):
            # SOAP ArrayOfX: a plain list stands for {"X": [...]}.
            issues = []
            for i, element in enumerate(value):
                if element is not None:
                    issues += self._check(
                        element, item, surface, profile, f"{label}[{i}]", where, depth + 1
                    )
            return issues
        if not isinstance(value, Mapping):
            return bad
        return self._check_object(value, type_name, schema, surface, profile, label, depth)

    @staticmethod
    def _check_enum(
        value: Any, type_name: str, schema: Mapping[str, Any], profile: str, label: str
    ) -> list[Issue]:
        values: Mapping[str, Mapping[str, Any]] = schema.get("values") or {}
        if not isinstance(value, str) or value not in values:
            lowered = {k.lower(): k for k in values}
            if isinstance(value, str) and value.lower() in lowered:
                return [
                    Issue(
                        "IR-3006",
                        label,
                        f"{label}: enum values are case-sensitive; use {lowered[value.lower()]!r}.",
                    )
                ]
            allowed = [k for k, v in values.items() if profile in v.get("availableIn", [profile])]
            return [
                Issue(
                    "IR-3006",
                    label,
                    f"{label} must be one of {', '.join(allowed)} ({type_name}), "
                    f"got {_describe(value)}.",
                )
            ]
        available_in = values[value].get("availableIn")
        if available_in is not None and profile not in available_in:
            return [
                Issue(
                    "IR-3007",
                    label,
                    f"{type_name}.{value} exists only in {', '.join(available_in)}, not {profile}.",
                )
            ]
        return []

    def _check_object(
        self,
        value: Mapping[str, Any],
        type_name: str,
        schema: Mapping[str, Any],
        surface: str,
        profile: str,
        label: str,
        depth: int,
    ) -> list[Issue]:
        fields = self._fields(surface, schema)
        issues: list[Issue] = []
        for key, item in value.items():
            spec = fields.get(key)
            if spec is None:
                issue = self._unknown(key, list(fields), f"field of {type_name}")
                issues.append(Issue(issue.code, f"{label}.{key}", issue.message))
                continue
            available_in = spec.get("availableIn")
            if available_in is not None and profile not in available_in:
                issues.append(
                    Issue(
                        "IR-3007",
                        f"{label}.{key}",
                        f"{type_name}.{key} exists only in {', '.join(available_in)}, "
                        f"not {profile}.",
                    )
                )
                continue
            if item is None:
                continue
            if spec.get("repeated") and isinstance(item, list):
                for i, element in enumerate(item):
                    if element is not None:
                        issues += self._check(
                            element,
                            str(spec["type"]),
                            surface,
                            profile,
                            f"{label}.{key}[{i}]",
                            "body",
                            depth + 1,
                        )
                continue
            issues += self._check(
                item, str(spec["type"]), surface, profile, f"{label}.{key}", "body", depth + 1
            )
        for key, spec in fields.items():
            if spec.get("required") and value.get(key) is None:
                available_in = spec.get("availableIn")
                if available_in is None or profile in available_in:
                    issues.append(Issue("IR-3005", f"{label}.{key}", f"{key} is required."))
        return issues


def _array_item(surface: str, type_name: str, schema: Mapping[str, Any]) -> str | None:
    """Item type of a SOAP ``ArrayOfX`` wrapper (one repeated field), else None."""
    fields = schema.get("fields") or {}
    if surface != "soap" or not type_name.startswith("ArrayOf") or len(fields) != 1:
        return None
    (spec,) = fields.values()
    return str(spec["type"]) if spec.get("repeated") else None


def _describe(value: Any) -> str:
    if value is None:
        return "null"
    text = repr(value)
    return f"{type(value).__name__} {text[:40]}"
