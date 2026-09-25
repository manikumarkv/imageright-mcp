"""Catalog service: offline lookups over the generated catalog (plan §4).

Everything here is pure data access. MCP handlers in ``imageright_mcp.tools`` validate input,
call one method, and wrap the returned :class:`Answer` in the standard envelope.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from imageright_mcp.catalog.areas import AREAS, area_of, match_area
from imageright_mcp.catalog.data import load_raw
from imageright_mcp.catalog.search import SearchIndex
from imageright_mcp.config import resolve_profile
from imageright_mcp.errors.registry import get_registry

SURFACES = ("rest-v1", "rest-v2", "soap")
PRESENT = frozenset({"available", "changed", "deprecated"})
ABSENT_PENALTY = 0.3
IR_CODE_NOTE = "irCode is the stable IR error code; ir_explain_error describes it."


class CatalogError(Exception):
    """Bad input to a catalog lookup; carries an IR-1xxx/3xxx code for the envelope."""

    def __init__(
        self, code: str, name: str, message: str, hint: str, suggestions: list[str] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.name = name
        self.message = message
        self.hint = hint
        self.suggestions = suggestions or []

    def to_error(self) -> dict[str, Any]:
        # Name stays as raised: to_envelope turns a code/name mismatch into IR-9001.
        error = get_registry().error(
            self.code, message=self.message, hint=self.hint, suggestions=self.suggestions
        )
        error["name"] = self.name
        return error


def warning(code: str, name: str, message: str) -> dict[str, str]:
    return {"code": code, "name": name, "message": message}


@dataclass
class Answer:
    data: Any
    warnings: list[dict[str, str]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


def _strip_array(type_name: str) -> str:
    return type_name[:-2] if type_name.endswith("[]") else type_name


def _invert_change(text: str) -> str:
    """Flip a version-diff note so it reads in the opposite direction ("+A -B" -> "-A +B")."""
    words = []
    for word in text.split(" "):
        if word.startswith("+"):
            word = "-" + word[1:]
        elif word.startswith("-"):
            word = "+" + word[1:]
        elif word == "added":
            word = "removed"
        elif word == "removed":
            word = "added"
        words.append(word)
    return " ".join(words)


class Catalog:
    def __init__(self, raw: dict[str, dict[str, Any]]) -> None:
        self.ops: dict[str, dict[str, Any]] = raw["operations"]["operations"]
        self.profiles: list[str] = list(raw["operations"]["profiles"])
        self.baseline: str = raw["operations"]["baseline"]
        self.schemas: dict[str, dict[str, dict[str, Any]]] = raw["schemas"]["schemas"]
        self.matrix = raw["matrix"]
        self.flows: dict[str, dict[str, Any]] = raw["flows"]["flows"]
        self.capabilities: dict[str, dict[str, Any]] = raw["capabilities"]["capabilities"]
        self.errors: dict[str, dict[str, Any]] = raw["errors"]["rest"]
        self.diff: dict[str, dict[str, dict[str, Any]]] = raw["version_diff"]["diff"]
        # Wire-level SOAP operation table (arg order, kinds, field sequences) for the M5 client.
        self.soap_table: dict[str, Any] = raw["soap_table"]
        self.area: dict[str, str] = {op_id: area_of(op) for op_id, op in self.ops.items()}
        self.index = SearchIndex(self.ops.values(), self.area, self._response_text())
        self._by_key: dict[str, str] = {
            str(op["key"]).lower(): op_id for op_id, op in self.ops.items()
        }
        self._by_lower: dict[str, str] = {op_id.lower(): op_id for op_id in self.ops}
        self._soap_names: dict[str, str] = {
            str(op["operation"]).lower(): op_id
            for op_id, op in self.ops.items()
            if op["surface"] == "soap"
        }
        self._path_patterns = [
            (op_id, str(op["method"]), re.compile(self._path_regex(str(op["requestPath"]))))
            for op_id, op in self.ops.items()
            if op["surface"] != "soap"
        ]
        self._field_meanings = self._harvest_field_meanings()

    # ------------------------------------------------------------------ resolution helpers

    def resolve_version(
        self, version: str | None, default: str
    ) -> tuple[str, list[dict[str, str]]]:
        """Map ``24.x``/``25.1``/``7.2``-style input (or the configured default) to a profile."""
        text = (version or default).strip()
        if text in self.profiles:
            return text, []
        resolution = resolve_profile(text)
        if resolution.profile is None:
            raise CatalogError(
                "IR-1002",
                "UnsupportedVersion",
                f"Version {text!r} does not map to a catalog profile.",
                f"Use one of {', '.join(self.profiles)} or a 24.x / 25.x / 7.2 style version.",
            )
        warnings = []
        if resolution.approximated:
            warnings.append(
                warning(
                    "IR-1005",
                    "ProfileApproximated",
                    f"Version {text} has no exact catalog profile; using {resolution.profile}.",
                )
            )
        return resolution.profile, warnings

    @staticmethod
    def resolve_surface(surface: str | None) -> str | None:
        if surface in (None, "", "any"):
            return None
        if surface not in SURFACES:
            raise CatalogError(
                "IR-3006",
                "InvalidParamValue",
                f"Unknown surface {surface!r}.",
                "Use rest-v1, rest-v2, soap or any.",
            )
        return surface

    @staticmethod
    def resolve_area(area: str | None) -> str | None:
        if not area:
            return None
        matched = match_area(area)
        if matched is None:
            raise CatalogError(
                "IR-3006",
                "InvalidParamValue",
                f"Unknown area {area!r}.",
                "Call ir_list_areas to see the area names.",
                difflib.get_close_matches(area, list(AREAS), n=3, cutoff=0.5),
            )
        return matched

    @staticmethod
    def _path_regex(path: str) -> str:
        return "^" + re.sub(r"\\\{[^}]*\\\}", r"[^/]+", re.escape(path.rstrip("/"))) + "/?$"

    def unknown_operation(self, ident: str) -> CatalogError:
        pool = list(self.ops) + list(self.capabilities)
        close = difflib.get_close_matches(ident, pool, n=5, cutoff=0.6)
        if not close:
            close = [op_id for op_id, _ in self.index.rank(ident)[:5]]
        return CatalogError(
            "IR-3001",
            "UnknownOperation",
            f"No operation or capability named {ident!r}.",
            "Use ir_search_apis to find the operationId, then call this tool again.",
            close,
        )

    def find_operation_id(self, ident: str) -> str | None:
        """Exact id, case-insensitive id, ``METHOD /path`` key, or a bare SOAP operation name."""
        text = ident.strip()
        if text in self.ops:
            return text
        lowered = text.lower()
        for table in (self._by_lower, self._by_key, self._soap_names):
            if lowered in table:
                return table[lowered]
        if lowered.startswith("soap:") and lowered[5:] in self._soap_names:
            return self._soap_names[lowered[5:]]
        return None

    def match_path(self, path: str, method: str | None) -> str:
        """Find the REST operation a concrete or templated path belongs to."""
        wanted_method = method.strip().upper() if method else None
        bare, _, query = path.strip().partition("?")
        bare = bare.rstrip("/") or "/"
        templated = re.sub(r"\{[^}]*\}", "{}", bare)
        hits: list[str] = []
        for op_id, op_method, pattern in self._path_patterns:
            if wanted_method and op_method != wanted_method:
                continue
            op_templated = re.sub(r"\{[^}]*\}", "{}", str(self.ops[op_id]["requestPath"]))
            if pattern.match(bare) or op_templated.lower() == templated.lower():
                hits.append(op_id)
        if len(hits) > 1:
            # Query-string overloads (e.g. GET /api/marks?fileTypeId) share a path.
            names = {part.split("=")[0].lower() for part in query.split("&") if part}
            overloads = [
                h
                for h in hits
                if "?" in str(self.ops[h]["key"])
                and str(self.ops[h]["key"]).split("?", 1)[1].lower() in names
            ]
            plain = [h for h in hits if "?" not in str(self.ops[h]["key"])]
            hits = overloads or plain or hits
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise CatalogError(
                "IR-3001",
                "UnknownOperation",
                f"No REST operation matches {wanted_method or 'any method'} {path}.",
                "Check the path (it starts with /api or /api/v2) or use ir_search_apis.",
            )
        raise CatalogError(
            "IR-3001",
            "UnknownOperation",
            f"{path} matches several operations; pass the method too.",
            "Repeat the call with method set, or use one of the suggested operationIds.",
            sorted(hits),
        )

    def _preferred_implementation(
        self, capability: dict[str, Any], profile: str, preference: list[str]
    ) -> str:
        impls: dict[str, dict[str, Any]] = capability["implementations"]
        order = [s for s in preference if s in impls] + [s for s in SURFACES if s in impls]
        for surface in dict.fromkeys(order):
            op = self.ops[impls[surface]["operationId"]]
            if op["availability"].get(profile) in {"available", "changed"}:
                return str(op["id"])
        return str(impls[next(iter(dict.fromkeys(order)))]["operationId"])

    def resolve(self, ident: str, profile: str, preference: list[str]) -> tuple[str, str | None]:
        """Resolve an operationId or capabilityId to ``(operationId, capabilityId)``."""
        op_id = self.find_operation_id(ident)
        if op_id is not None:
            return op_id, self.ops[op_id].get("capability")
        capability = self.capabilities.get(ident.strip())
        if capability is not None:
            return self._preferred_implementation(capability, profile, preference), capability["id"]
        raise self.unknown_operation(ident)

    # ------------------------------------------------------------------ small views

    def _op_brief(self, op_id: str, profile: str | None = None) -> dict[str, Any]:
        op = self.ops[op_id]
        brief: dict[str, Any] = {"operationId": op_id, "surface": op["surface"]}
        if op["surface"] == "soap":
            brief["soapOperation"] = op["operation"]
        else:
            brief["method"] = op["method"]
            brief["path"] = op["path"]
        brief["summary"] = op["summary"]
        if profile is not None:
            brief["availability"] = op["availability"].get(profile, "absent")
        return brief

    def _error_entry(self, code: int) -> dict[str, Any]:
        entry = self.errors.get(str(code))
        registry = get_registry()
        ir_code = registry.for_native_rest(code)
        return {
            "nativeCode": code,
            "name": entry["name"] if entry else None,
            "family": entry["family"] if entry else None,
            "irCode": ir_code,
            "irName": registry.entry(ir_code)["name"],
        }

    def _schema(self, surface: str, name: str) -> dict[str, Any] | None:
        return self.schemas.get(surface, {}).get(_strip_array(name))

    @staticmethod
    def _in_profile(item: dict[str, Any], profile: str) -> bool:
        available_in = item.get("availableIn")
        return available_in is None or profile in available_in

    def _enum_values(self, surface: str, type_name: str, profile: str) -> list[str] | None:
        schema = self._schema(surface, type_name)
        if not schema or schema.get("kind") != "enum":
            return None
        return [str(v["value"]) for v in schema["values"].values() if self._in_profile(v, profile)]

    def _response_text(self) -> dict[str, str]:
        """Field names of each operation's success response, so search can match returned data."""
        text: dict[str, str] = {}
        for op_id, op in self.ops.items():
            if op["surface"] == "soap":
                types = [str((op.get("result") or {}).get("type") or "")]
            else:
                types = [
                    str(r.get("type") or "")
                    for status, r in (op.get("responses") or {}).items()
                    if status.startswith("2")
                ]
            names = [
                field_name
                for t in types
                for field_name in ((self._schema(op["surface"], t) or {}).get("fields") or {})
            ]
            text[op_id] = " ".join(names)
        return text

    def _harvest_field_meanings(self) -> dict[tuple[str, str, str], str]:
        """Plain-English meanings for schema fields, borrowed from annotated body params."""
        meanings: dict[tuple[str, str, str], str] = {}
        for op in self.ops.values():
            body = op.get("requestBody") or {}
            schema = body.get("type")
            if not schema:
                continue
            for param in op["params"]:
                if param["in"] == "body" and param.get("meaning"):
                    key = (op["surface"], _strip_array(str(schema)), str(param["name"]))
                    meanings.setdefault(key, str(param["meaning"]))
        return meanings

    # ------------------------------------------------------------------ explorer tools

    def search(
        self,
        query: str,
        profile: str,
        surface: str | None = None,
        area: str | None = None,
        include_deprecated: bool = False,
        limit: int = 10,
    ) -> Answer:
        if not query.strip():
            raise CatalogError(
                "IR-3006", "InvalidParamValue", "The query is empty.", "Describe what you want."
            )
        allowed = {
            op_id
            for op_id, op in self.ops.items()
            if (surface is None or op["surface"] == surface)
            and (area is None or self.area[op_id] == area)
            and (include_deprecated or op.get("deprecation") is None)
        }
        ranked = []
        for op_id, score in self.index.rank(query, allowed):
            if self.ops[op_id]["availability"].get(profile, "absent") not in PRESENT:
                score *= ABSENT_PENALTY
            ranked.append((op_id, score))
        ranked.sort(key=lambda r: (-r[1], r[0]))
        top = ranked[0][1] if ranked else 1.0
        results = []
        for op_id, score in ranked[:limit]:
            op = self.ops[op_id]
            item = self._op_brief(op_id, profile)
            item["capabilityId"] = op.get("capability")
            item["area"] = self.area[op_id]
            item["deprecated"] = op.get("deprecation") is not None
            item["safety"] = op["safety"]
            item["score"] = round(score / top, 3)
            results.append(item)
        answer = Answer(
            data={"query": query, "version": profile, "total": len(ranked), "results": results}
        )
        if not results:
            answer.data["hint"] = (
                "Nothing matched. Try other words, drop the surface/area filter, "
                "or browse with ir_list_areas."
            )
        return answer

    def list_areas(self, profile: str, surface: str | None = None) -> Answer:
        grouped: dict[str, list[str]] = {name: [] for name in AREAS}
        for op_id, op in self.ops.items():
            if op["availability"].get(profile, "absent") in PRESENT:
                grouped[self.area[op_id]].append(op_id)
        areas = []
        for name, op_ids in grouped.items():
            counts = {s: 0 for s in SURFACES}
            for op_id in op_ids:
                counts[self.ops[op_id]["surface"]] += 1
            pool = [o for o in op_ids if surface is None or self.ops[o]["surface"] == surface]
            if not pool:
                continue
            pool.sort(
                key=lambda o: (
                    self.ops[o].get("deprecation") is not None,
                    not self.ops[o].get("annotated"),
                    not self.ops[o].get("capability"),
                    o,
                )
            )
            areas.append(
                {
                    "area": name,
                    "description": AREAS[name],
                    "counts": counts,
                    "topOperations": [
                        {"operationId": o, "summary": self.ops[o]["summary"]} for o in pool[:5]
                    ],
                }
            )
        return Answer(data={"version": profile, "surface": surface or "any", "areas": areas})

    def _param_view(
        self, op: dict[str, Any], param: dict[str, Any], profile: str
    ) -> dict[str, Any]:
        name = str(param["name"])
        view: dict[str, Any] = {
            "name": name,
            "in": param["in"],
            "type": param["type"],
            "required": bool(param.get("required")),
        }
        if "default" in param:
            view["default"] = param["default"]
        if param.get("nullable"):
            view["nullable"] = True
        allowed = self._enum_values(op["surface"], str(param["type"]), profile)
        if allowed is not None:
            view["allowedValues"] = allowed
        if param.get("token"):
            view["meaning"] = (
                "Session token from UserLogin. It rotates: each response carries the token for "
                "the next call."
            )
        else:
            view["meaning"] = param.get("meaning")
        if param.get("valueFrom"):
            view["valueFrom"] = [
                {
                    "operationId": src["op"],
                    "field": src.get("field"),
                    **({"note": src["note"]} if src.get("note") else {}),
                    **(
                        {"summary": self.ops[src["op"]]["summary"]} if src["op"] in self.ops else {}
                    ),
                }
                for src in param["valueFrom"]
            ]
        available_in = op.get("paramAvailability", {}).get(name)
        if available_in is not None:
            view["availableIn"] = available_in
            view["availableInVersion"] = profile in available_in
        return view

    def _response_view(self, op: dict[str, Any], profile: str) -> dict[str, Any]:
        if op["surface"] == "soap":
            result = op.get("result")
            if not result:
                return {"type": None}
            view = {"element": result["name"], "type": result["type"]}
            view.update(self._fields_brief("soap", str(result["type"]), profile))
            return view
        responses: dict[str, Any] = {}
        for status, resp in sorted(op.get("responses", {}).items()):
            entry: dict[str, Any] = {
                "type": resp.get("type"),
                "contentType": resp.get("contentType"),
            }
            if status.startswith("2") and resp.get("type"):
                entry.update(self._fields_brief(op["surface"], str(resp["type"]), profile))
            responses[status] = entry
        return responses

    def _fields_brief(self, surface: str, type_name: str, profile: str) -> dict[str, Any]:
        schema = self._schema(surface, type_name)
        if not schema or not schema.get("fields"):
            return {}
        return {
            "fields": {
                name: spec["type"]
                for name, spec in schema["fields"].items()
                if self._in_profile(spec, profile)
            }
        }

    def _related(self, op_id: str) -> list[dict[str, Any]]:
        op = self.ops[op_id]
        related: dict[str, str] = {}
        cap_id = op.get("capability")
        if cap_id and cap_id in self.capabilities:
            for impl in self.capabilities[cap_id]["implementations"].values():
                related.setdefault(impl["operationId"], f"same capability ({cap_id})")
        if op.get("deprecation"):
            related.setdefault(op["deprecation"]["replacement"], "replacement")
        for other_id, other in self.ops.items():
            dep = other.get("deprecation")
            if dep and dep.get("replacement") == op_id:
                related.setdefault(other_id, "deprecated predecessor")
        for param in op["params"]:
            for src in param.get("valueFrom") or []:
                related.setdefault(src["op"], f"supplies {param['name']}")
        for flow_id in op.get("flows") or []:
            steps = [s["operationId"] for s in self.flows[flow_id]["steps"]]
            for i, step_op in enumerate(steps):
                if step_op != op_id:
                    continue
                if i > 0:
                    related.setdefault(steps[i - 1], f"previous step in {flow_id}")
                if i + 1 < len(steps):
                    related.setdefault(steps[i + 1], f"next step in {flow_id}")
        related.pop(op_id, None)
        return [
            {"operationId": other, "relation": why, "summary": self.ops[other]["summary"]}
            for other, why in related.items()
            if other in self.ops
        ][:12]

    def describe_api(self, ident: str, profile: str, preference: list[str] | None = None) -> Answer:
        op_id, cap_id = self.resolve(ident, profile, preference or list(SURFACES))
        op = self.ops[op_id]
        status = op["availability"].get(profile, "absent")
        data: dict[str, Any] = {
            "operationId": op_id,
            "capabilityId": op.get("capability"),
            "surface": op["surface"],
            "summary": op["summary"],
            "explanation": op.get("explanation"),
            "area": self.area[op_id],
            "safety": op["safety"],
            "auth": op["auth"],
        }
        if op["surface"] == "soap":
            data["request"] = {
                "soapOperation": op["operation"],
                "soapAction": op["soapAction"],
                "requestElement": op["requestElement"],
                "argumentOrder": [
                    p["name"] for p in sorted(op["params"], key=lambda p: p["order"])
                ],
                "tokenRotation": bool(op.get("tokenRotation")),
            }
        else:
            data["request"] = {"method": op["method"], "path": op["path"]}
        data["params"] = [
            self._param_view(op, p, profile) for p in op["params"] if p["in"] != "multipart"
        ]
        body = op.get("requestBody")
        if body:
            data["requestBody"] = {
                "type": body.get("type"),
                "contentType": body.get("contentType"),
                "required": bool(body.get("required")),
                "note": "Body fields are listed in params with in=body (expanded one level).",
            }
        parts = [p for p in op["params"] if p["in"] == "multipart"]
        if parts:
            data["multipartParts"] = [
                {"name": p["name"], "type": p["type"], "meaning": p.get("meaning")} for p in parts
            ]
        data["response"] = self._response_view(op, profile)
        data["errors"] = [self._error_entry(int(code)) for code in op.get("errors") or []]
        if data["errors"]:
            data["errorsNote"] = IR_CODE_NOTE
        data["gotchas"] = list(op.get("gotchas") or [])
        data["availability"] = {
            "version": profile,
            "status": status,
            "row": op["availability"],
            "changes": op.get("changes") or {},
        }
        data["deprecation"] = op.get("deprecation")
        data["related"] = self._related(op_id)
        data["flows"] = [
            {"flowId": f, "title": self.flows[f]["title"]} for f in op.get("flows") or []
        ]
        data["source"] = {
            **op["source"],
            "confidence": "annotated" if op.get("annotated") else "generated",
            "confidenceNote": (
                "Summary, meanings and gotchas were written and checked by hand."
                if op.get("annotated")
                else "Generated from the published API description only; no hand review yet."
            ),
        }
        answer = Answer(data=data, meta={"operationId": op_id, "profile": profile})
        if cap_id and ident.strip() == cap_id:
            cap = self.capabilities[cap_id]
            data["capability"] = {
                "id": cap_id,
                "summary": cap["summary"],
                "implementations": [
                    {
                        "surface": surface,
                        "operationId": impl["operationId"],
                        "verified": impl.get("verified", False),
                        "availability": self.ops[impl["operationId"]]["availability"].get(profile),
                    }
                    for surface, impl in cap["implementations"].items()
                ],
            }
            answer.meta["capabilityId"] = cap_id
        if status == "absent":
            answer.warnings.append(
                warning(
                    "IR-3002",
                    "OperationNotAvailableInVersion",
                    f"{op_id} is not published for {profile}.",
                )
            )
        if op.get("deprecation"):
            answer.warnings.append(
                warning(
                    "IR-3003",
                    "OperationDeprecated",
                    f"{op_id} is deprecated; use {op['deprecation']['replacement']}.",
                )
            )
        return answer

    def _find_types(self, name: str, surface: str | None) -> list[tuple[str, str]]:
        wanted = _strip_array(name.strip())
        surfaces = [surface] if surface else list(SURFACES)
        hits = [(s, wanted) for s in surfaces if wanted in self.schemas.get(s, {})]
        if not hits:
            lowered = wanted.lower()
            hits = [
                (s, n) for s in surfaces for n in self.schemas.get(s, {}) if n.lower() == lowered
            ]
        return hits

    def describe_type(self, name: str, profile: str, surface: str | None = None) -> Answer:
        hits = self._find_types(name, surface)
        if not hits:
            pool = sorted({n for s in SURFACES for n in self.schemas.get(s, {})})
            raise CatalogError(
                "IR-3006",
                "InvalidParamValue",
                f"No schema or enum named {name!r}" + (f" on {surface}." if surface else "."),
                "Type names come from ir_describe_api (param, body and response types).",
                difflib.get_close_matches(name, pool, n=5, cutoff=0.6),
            )
        definitions: list[dict[str, Any]] = []
        for surf, type_name in hits:
            view = self._type_view(surf, type_name, profile)
            for existing in definitions:
                if existing["name"] == type_name and existing["_raw"] == view["_raw"]:
                    existing["surfaces"].append(surf)
                    break
            else:
                definitions.append(view)
        for view in definitions:
            view.pop("_raw")
        return Answer(data={"version": profile, "definitions": definitions})

    def _type_view(self, surface: str, name: str, profile: str) -> dict[str, Any]:
        schema = self.schemas[surface][name]
        kind = schema.get("kind") or ("object" if "fields" in schema else "unknown")
        view: dict[str, Any] = {
            "name": name,
            "surfaces": [surface],
            "kind": kind,
            "availableIn": schema.get("availableIn", self.profiles),
            "availableInVersion": self._in_profile(schema, profile),
            "_raw": schema,
        }
        if schema.get("extends"):
            view["extends"] = schema["extends"]
        differences: list[str] = []
        if "fields" in schema:
            fields = []
            for field_name, spec in schema["fields"].items():
                item: dict[str, Any] = {
                    "name": field_name,
                    "type": spec["type"],
                    "required": bool(spec.get("required")),
                }
                if spec.get("nullable"):
                    item["nullable"] = True
                if spec.get("repeated"):
                    item["repeated"] = True
                meaning = self._field_meanings.get((surface, name, field_name))
                if meaning:
                    item["meaning"] = meaning
                allowed = self._enum_values(surface, str(spec["type"]), profile)
                if allowed is not None:
                    item["allowedValues"] = allowed
                if "availableIn" in spec:
                    item["availableIn"] = spec["availableIn"]
                    item["availableInVersion"] = profile in spec["availableIn"]
                    differences.append(f"{field_name}: only in {', '.join(spec['availableIn'])}")
                if "byProfile" in spec:
                    by_type = {p: v["type"] for p, v in sorted(spec["byProfile"].items())}
                    item["typeByVersion"] = by_type
                    item["type"] = by_type.get(profile, spec["type"])
                    differences.append(
                        f"{field_name}: type "
                        + ", ".join(f"{t} in {p}" for p, t in by_type.items())
                    )
                fields.append(item)
            view["fields"] = fields
        if "values" in schema:
            values = []
            for value_name, spec in schema["values"].items():
                item = {"name": value_name, "value": spec["value"]}
                if "availableIn" in spec:
                    item["availableIn"] = spec["availableIn"]
                    item["availableInVersion"] = profile in spec["availableIn"]
                    differences.append(
                        f"value {value_name}: only in {', '.join(spec['availableIn'])}"
                    )
                values.append(item)
            view["values"] = values
        view["versionDifferences"] = differences
        view["usedBy"] = self._used_by(surface, name)
        return view

    def _used_by(self, surface: str, name: str, limit: int = 15) -> list[str]:
        users = []
        for op_id, op in self.ops.items():
            if op["surface"] != surface:
                continue
            types = [str(p["type"]) for p in op["params"]]
            types += [str((op.get("requestBody") or {}).get("type") or "")]
            types += [str(r.get("type") or "") for r in (op.get("responses") or {}).values()]
            types += [str((op.get("result") or {}).get("type") or "")]
            if name in {_strip_array(t) for t in types}:
                users.append(op_id)
        return sorted(users)[:limit]

    def _flow(self, flow_id: str) -> dict[str, Any]:
        flow = self.flows.get(flow_id.strip()) or next(
            (f for fid, f in self.flows.items() if fid.lower() == flow_id.strip().lower()), None
        )
        if flow is None:
            raise CatalogError(
                "IR-3006",
                "InvalidParamValue",
                f"No flow named {flow_id!r}.",
                "Call ir_list_flows to see the flow ids.",
                sorted(self.flows),
            )
        return flow

    def _flow_surfaces(self, flow: dict[str, Any]) -> list[str]:
        surfaces = {self.ops[s["operationId"]]["surface"] for s in flow["steps"]}
        return [s for s in SURFACES if s in surfaces]

    def _flow_caveats(self, flow: dict[str, Any], profile: str) -> list[str]:
        caveats = list(flow.get("versionCaveats") or [])
        for step in flow["steps"]:
            op = self.ops[step["operationId"]]
            status = op["availability"].get(profile, "absent")
            if status == "absent":
                caveats.append(f"Step {step['n']} ({op['id']}) is not published for {profile}.")
            elif status == "changed":
                notes = "; ".join(op.get("changes", {}).get(profile, []))
                caveats.append(f"Step {step['n']} ({op['id']}) differs in {profile}: {notes}")
            elif status == "deprecated":
                caveats.append(f"Step {step['n']} ({op['id']}) is deprecated.")
        return caveats

    def list_flows(self, profile: str, surface: str | None = None) -> Answer:
        flows = []
        for flow_id, flow in self.flows.items():
            surfaces = self._flow_surfaces(flow)
            if surface and surface not in surfaces:
                continue
            flows.append(
                {
                    "flowId": flow_id,
                    "title": flow["title"],
                    "purpose": flow["purpose"],
                    "steps": len(flow["steps"]),
                    "surfaces": surfaces,
                    "usableInVersion": all(
                        self.ops[s["operationId"]]["availability"].get(profile) in PRESENT
                        for s in flow["steps"]
                    ),
                }
            )
        return Answer(data={"version": profile, "flows": flows})

    def describe_flow(self, flow_id: str, profile: str, surface: str | None = None) -> Answer:
        flow = self._flow(flow_id)
        surfaces = self._flow_surfaces(flow)
        answer = Answer(data={})
        if surface and surface not in surfaces:
            others = [fid for fid, f in self.flows.items() if surface in self._flow_surfaces(f)]
            answer.warnings.append(
                warning(
                    "IR-3004",
                    "NoSurfaceAvailable",
                    f"Flow {flow['id']} runs on {', '.join(surfaces)}, not {surface}. "
                    f"Flows on {surface}: {', '.join(others) or 'none'}.",
                )
            )
        steps = []
        failure_points = []
        for step in flow["steps"]:
            op = self.ops[step["operationId"]]
            item: dict[str, Any] = {"n": step["n"], **self._op_brief(op["id"], profile)}
            item["purpose"] = step["purpose"]
            item["safety"] = step.get("safety", op["safety"])
            item["params"] = step.get("params", {})
            item["inputsFrom"] = [i["ref"] for i in step.get("inputsFrom", [])]
            item["requiredParams"] = [
                p["name"] for p in op["params"] if p.get("required") and not p.get("token")
            ]
            for key in ("output", "optional", "repeat", "alternatives"):
                if key in step:
                    item[key] = step[key]
            steps.append(item)
            native = [self._error_entry(int(c)) for c in op.get("errors") or []]
            if step.get("onError") or native:
                failure_points.append(
                    {"step": step["n"], "onError": step.get("onError"), "errors": native}
                )
        answer.data = {
            "flowId": flow["id"],
            "title": flow["title"],
            **({"composite": flow["composite"]} if "composite" in flow else {}),
            "purpose": flow["purpose"],
            "version": profile,
            "surfaces": surfaces,
            "executes": False,
            "note": "Documentation only: this describes the calls; it does not run them.",
            "inputs": flow.get("inputs", []),
            "outputs": flow.get("outputs", {}),
            "steps": steps,
            "failurePoints": failure_points,
            "errorsNote": IR_CODE_NOTE,
            "versionCaveats": self._flow_caveats(flow, profile),
            "notes": flow.get("notes", []),
        }
        return answer

    # ------------------------------------------------------------------ version-matrix tools

    def _member_availability(self, surface: str, spec: str) -> tuple[str, str, dict[str, Any]]:
        """Look up ``Type.Member`` (schema field or enum value) on a surface."""
        type_name, _, member = spec.partition(".")
        schema = self._schema(surface, type_name)
        if schema is None or not member:
            raise KeyError(spec)
        pool = {**(schema.get("fields") or {}), **(schema.get("values") or {})}
        for key, value in pool.items():
            if key.lower() == member.lower():
                return type_name, key, value
        raise KeyError(spec)

    def check_availability(
        self,
        profiles: list[str],
        ident: str | None = None,
        path: str | None = None,
        method: str | None = None,
        param: str | None = None,
        preference: list[str] | None = None,
    ) -> Answer:
        if ident is None and path is None:
            if param and "." in param:
                return self._check_member(profiles, param)
            raise CatalogError(
                "IR-3005",
                "MissingRequiredParam",
                "Say what to check.",
                "Pass operationId or capabilityId, or path (+ method), or param as Type.Member.",
            )
        if ident is not None:
            cap = self.capabilities.get(ident.strip())
            if cap is not None and param is None:
                return self._check_capability(cap, profiles)
            op_id, _ = self.resolve(ident, profiles[0], preference or list(SURFACES))
        else:
            op_id = self.match_path(str(path), method)
        op = self.ops[op_id]
        rows = []
        for profile in profiles:
            status = op["availability"].get(profile, "absent")
            row: dict[str, Any] = {"version": profile, "status": status}
            notes = (op.get("changes") or {}).get(profile)
            if notes:
                row["notes"] = notes
            if op.get("deprecation") and profile in op["deprecation"]["in"]:
                row["replacement"] = op["deprecation"]["replacement"]
                row["notes"] = [*row.get("notes", []), op["deprecation"]["note"]]
            rows.append(row)
        data: dict[str, Any] = {**self._op_brief(op_id), "rows": rows}
        if param is not None:
            data["param"] = self._check_param(op, param, profiles, rows)
        return Answer(data=data, meta={"operationId": op_id})

    def _check_param(
        self, op: dict[str, Any], param: str, profiles: list[str], op_rows: list[dict[str, Any]]
    ) -> dict[str, Any]:
        if "." in param:
            try:
                type_name, member, spec = self._member_availability(op["surface"], param)
            except KeyError:
                raise CatalogError(
                    "IR-3006",
                    "InvalidParamValue",
                    f"{param} is not a field or enum value on {op['surface']}.",
                    "Use Type.Member, e.g. TaskFilterV2.FileId.",
                ) from None
            available_in = spec.get("availableIn") or self.profiles
            return {
                "name": f"{type_name}.{member}",
                "rows": [
                    {"version": p, "status": "available" if p in available_in else "absent"}
                    for p in profiles
                ],
            }
        names = {str(p["name"]).lower(): str(p["name"]) for p in op["params"]}
        real = names.get(param.strip().lower())
        if real is None:
            raise CatalogError(
                "IR-3006",
                "InvalidParamValue",
                f"{op['id']} has no parameter {param!r}.",
                "Check the parameter names with ir_describe_api.",
                difflib.get_close_matches(param, list(names.values()), n=5, cutoff=0.5),
            )
        available_in = op.get("paramAvailability", {}).get(real)
        rows = []
        for op_row in op_rows:
            profile = op_row["version"]
            present = op_row["status"] in PRESENT and (
                available_in is None or profile in available_in
            )
            rows.append({"version": profile, "status": "available" if present else "absent"})
        return {"name": real, "rows": rows}

    def _check_member(self, profiles: list[str], spec: str) -> Answer:
        definitions = []
        for surface in SURFACES:
            try:
                type_name, member, item = self._member_availability(surface, spec)
            except KeyError:
                continue
            schema_in = self.schemas[surface][type_name].get("availableIn") or self.profiles
            available_in = [p for p in item.get("availableIn") or self.profiles if p in schema_in]
            definitions.append(
                {
                    "surface": surface,
                    "name": f"{type_name}.{member}",
                    "rows": [
                        {"version": p, "status": "available" if p in available_in else "absent"}
                        for p in profiles
                    ],
                }
            )
        if not definitions:
            raise CatalogError(
                "IR-3006",
                "InvalidParamValue",
                f"{spec} is not a known schema field or enum value.",
                "Use Type.Member, e.g. TaskFilterV2.FileId or "
                "TaskAgeCalculationAlgorithm.AvailableDate.",
            )
        return Answer(data={"member": spec, "definitions": definitions})

    def _check_capability(self, cap: dict[str, Any], profiles: list[str]) -> Answer:
        rows = [{"version": p, "status": cap["availability"].get(p, "absent")} for p in profiles]
        impls = [
            {
                "surface": surface,
                "operationId": impl["operationId"],
                "verified": impl.get("verified", False),
                "row": {
                    p: self.ops[impl["operationId"]]["availability"].get(p, "absent")
                    for p in profiles
                },
            }
            for surface, impl in cap["implementations"].items()
        ]
        return Answer(
            data={
                "capabilityId": cap["id"],
                "summary": cap["summary"],
                "rows": rows,
                "implementations": impls,
            },
            meta={"capabilityId": cap["id"]},
        )

    def compare_versions(
        self, source: str, target: str, surface: str | None = None, area: str | None = None
    ) -> Answer:
        surfaces = [surface] if surface else list(SURFACES)
        result: dict[str, Any] = {"from": source, "to": target, "surfaces": {}}
        error_codes: dict[str, list[dict[str, Any]]] = {"added": [], "removed": []}
        for surf in surfaces:
            if surf not in self.diff:
                result["surfaces"][surf] = {
                    "note": "One WSDL serves every version, so there are no differences."
                }
                continue
            diff, inverted = self._pair(surf, source, target)
            result["surfaces"][surf] = self._diff_view(diff, inverted, area)
            codes = diff.get("errorCodes", {"added": [], "removed": []})
            added, removed = (
                (codes["removed"], codes["added"])
                if inverted
                else (
                    codes["added"],
                    codes["removed"],
                )
            )
            error_codes = {
                "added": [self._error_entry(int(c)) for c in added],
                "removed": [self._error_entry(int(c)) for c in removed],
            }
        result["errorCodes"] = error_codes
        result["errorCodesNote"] = "REST v1 and v2 share one error-code dictionary."
        if area:
            result["area"] = area
            result["areaNote"] = "The area filter applies to operations and parameters only."
        return Answer(data=result)

    def _pair(self, surface: str, source: str, target: str) -> tuple[dict[str, Any], bool]:
        if source == target:
            return {}, False
        diffs = self.diff[surface]
        if f"{source}->{target}" in diffs:
            return diffs[f"{source}->{target}"], False
        return diffs[f"{target}->{source}"], True

    def _diff_view(self, diff: dict[str, Any], inverted: bool, area: str | None) -> dict[str, Any]:
        added = diff.get("removedOperations" if inverted else "addedOperations", [])
        removed = diff.get("addedOperations" if inverted else "removedOperations", [])

        def keep(op_id: str) -> bool:
            return area is None or self.area.get(op_id) == area

        def flip(text: str) -> str:
            return _invert_change(text) if inverted else text

        changed_params = []
        for op_id, change in sorted(diff.get("changedParams", {}).items()):
            if not keep(op_id):
                continue
            plus, minus = ("removed", "added") if inverted else ("added", "removed")
            changed_params.append(
                {
                    "operationId": op_id,
                    "added": change[plus],
                    "removed": change[minus],
                    "modified": change["modified"],
                }
            )
        schemas_added = diff.get("removedSchemas" if inverted else "addedSchemas", [])
        schemas_removed = diff.get("addedSchemas" if inverted else "removedSchemas", [])
        return {
            "added": [self._op_brief(o) for o in added if keep(o)],
            "removed": [self._op_brief(o) for o in removed if keep(o)],
            "changedParams": changed_params,
            "changedSchemas": [
                {"name": n, "change": flip(c)}
                for n, c in sorted(diff.get("changedSchemas", {}).items())
            ]
            + [{"name": n, "change": "added"} for n in schemas_added]
            + [{"name": n, "change": "removed"} for n in schemas_removed],
            "changedEnums": [
                {"name": n, "change": flip(c)}
                for n, c in sorted(diff.get("changedEnums", {}).items())
            ],
        }

    def list_deprecations(self, profile: str | None = None) -> Answer:
        items = []
        for dep in self.matrix["deprecations"]:
            if profile and profile not in dep["deprecatedIn"]:
                continue
            op = self.ops[dep["operationId"]]
            replacement = self.ops.get(dep["replacement"])
            items.append(
                {
                    **self._op_brief(dep["operationId"]),
                    "deprecatedIn": dep["deprecatedIn"],
                    "replacement": dep["replacement"],
                    "replacementKey": replacement["key"] if replacement else None,
                    "replacementSummary": replacement["summary"] if replacement else None,
                    "note": dep["note"],
                    "source": op["deprecation"].get("source"),
                }
            )
        return Answer(data={"version": profile or "all", "deprecations": items})


@cache
def _cached(directory: Path | None) -> Catalog:
    return Catalog(load_raw(directory))


def get_catalog(directory: Path | None = None) -> Catalog:
    """The process-wide catalog, loaded once on first use."""
    return _cached(directory)
