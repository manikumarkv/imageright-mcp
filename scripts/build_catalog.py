"""Build the offline ImageRight catalog under ``data/catalog/`` (plan §5, milestone M1).

Inputs:
  * vendor snapshots (outside the repo; see ``scripts/fetch_vendor.py``): six OpenAPI documents
    (7.2, 24.2, 25.1 x REST v1/v2) and the SOAP reference pages with their embedded XSD sources;
  * the research report (only its structure is read: operation lists, SOAP areas, error lists),
    used to cross-check its claims against the mechanical diff;
  * hand-written ``annotations/*.yaml``: our own summaries, parameter meanings, value sources,
    safety levels, gotchas, flows and capabilities.

Only structural facts (names, paths, types, required flags, codes) are taken from vendor files.
Every human-facing sentence in the output comes from ``annotations/`` or is generated from names.
Vendor description text is never copied.

Output is deterministic: sorted keys, no timestamps, no absolute paths. ``--check`` rebuilds in
memory and fails if any committed file differs.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

Json = Any

PROFILES = ("7.2", "24.2", "25.1")
BASELINE = "24.2"
REST_SURFACES = {"rest-v1": "v1", "rest-v2": "v2"}
SURFACES = ("rest-v1", "rest-v2", "soap")
METHODS = ("get", "head", "post", "put", "patch", "delete")
SAFETY_LEVELS = ("read", "write", "destructive")
SOAP_NS = "http://imageright.com/imageright.webservice"
SOAP_PAGE_PREFIX = "http---imageright.com-imageright.webservice_xsd"
XS = "{http://www.w3.org/2001/XMLSchema}"
# Excluded from per-operation signature diffs: error codes are tracked on their own.
SIGNATURE_IGNORED_SCHEMAS = frozenset({"ErrorModel", "ErrorCodes"})

REPO = Path(__file__).resolve().parent.parent
DEFAULT_VENDOR = REPO.parent / "imageright-mcp-vendor"
DEFAULT_REPORT = (
    REPO.parent / "research_notes" / "imageright-api-catalog-20260924-1844" / "report.md"
)
ANNOTATIONS_DIR = REPO / "annotations"
OUTPUT_DIR = REPO / "data" / "catalog"
ERRORS_DIR = REPO / "data" / "errors"
# Hand-written error files in ERRORS_DIR: checked for copied vendor prose, never generated.
HAND_WRITTEN_ERROR_FILES = ("registry.json", "soap-faults.json")

# Our own family labels for the native REST error-code ranges.
ERROR_FAMILIES: tuple[tuple[int, int, str], ...] = (
    (1, 99, "auth-security-general"),
    (100, 199, "notes"),
    (200, 299, "tasks"),
    (300, 399, "workflow"),
    (400, 499, "documents"),
    (500, 599, "files"),
    (600, 699, "attributes"),
    (700, 799, "pages"),
    (800, 899, "drawers"),
    (900, 999, "general-objects"),
    (1000, 1099, "object-types"),
    (1100, 1199, "folders"),
    (1200, 1299, "marks"),
    (1300, 1399, "users"),
    (1400, 1499, "groups-roles"),
    (1500, 1599, "functionality-rights"),
    (1600, 1699, "sla"),
    (1700, 1799, "dashboard"),
    (1800, 1899, "vsso"),
    (1900, 1999, "data-validation"),
    (2000, 2099, "ocr"),
    (2100, 2199, "redaction"),
    (2200, 2299, "document-filters"),
    (2300, 2399, "config"),
    (2400, 2499, "email"),
    (2500, 2599, "task-history"),
    (2600, 2699, "annotations"),
)


class BuildError(Exception):
    """Raised when inputs are missing or annotations are inconsistent with the vendor data."""


# --------------------------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Inputs:
    oas: dict[str, dict[str, Json]]  # surface -> profile -> OpenAPI document
    soap_index: str
    soap_pages: dict[str, str]  # file name -> html
    report: str
    annotations: dict[str, Json]  # annotation file stem -> parsed yaml
    sources: dict[str, Json]
    error_texts: dict[str, Json] = field(default_factory=dict)  # hand-written data/errors files


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_inputs(
    vendor: Path, report: Path, annotations: Path, errors_dir: Path = ERRORS_DIR
) -> Inputs:
    if not vendor.is_dir():
        raise BuildError(f"vendor directory not found: {vendor} (run scripts/fetch_vendor.py)")
    if not report.is_file():
        raise BuildError(f"research report not found: {report}")
    sources: dict[str, Json] = {"oas": {}, "soap": {}, "report": {}, "annotations": {}}
    oas: dict[str, dict[str, Json]] = {}
    for surface, api in REST_SURFACES.items():
        oas[surface] = {}
        for profile in PROFILES:
            path = vendor / "oas" / profile / f"{api}.json"
            raw = path.read_bytes()
            oas[surface][profile] = json.loads(raw.decode("utf-8-sig"))
            sources["oas"][f"{profile}/{api}"] = {"sha256": _sha256(raw)}
    soap_dir = vendor / "soap"
    index_raw = (soap_dir / "index.html").read_bytes()
    pages: dict[str, str] = {}
    digest = hashlib.sha256()
    for page in sorted((soap_dir / "html").glob("*.html")):
        raw = page.read_bytes()
        pages[page.name] = raw.decode("utf-8-sig")
        digest.update(page.name.encode() + b"\0" + raw + b"\0")
    sources["soap"] = {
        "index": {"sha256": _sha256(index_raw)},
        "pages": {"count": len(pages), "sha256": digest.hexdigest()},
    }
    report_raw = report.read_bytes()
    sources["report"] = {"file": report.name, "sha256": _sha256(report_raw)}
    parsed: dict[str, Json] = {}
    for file in sorted(annotations.glob("*.yaml")):
        raw = file.read_bytes()
        parsed[file.stem] = yaml.safe_load(raw)
        sources["annotations"][file.name] = {"sha256": _sha256(raw)}
    manifest = vendor / "MANIFEST.json"
    if manifest.is_file():
        urls = json.loads(manifest.read_text())
        sources["urls"] = {
            rel: meta["url"] for rel, meta in urls.items() if not rel.startswith("soap/html/")
        }
    return Inputs(
        oas=oas,
        soap_index=index_raw.decode("utf-8-sig"),
        soap_pages=pages,
        report=report_raw.decode("utf-8"),
        annotations=parsed,
        sources=sources,
        error_texts={
            name: json.loads((errors_dir / name).read_text(encoding="utf-8"))
            for name in HAND_WRITTEN_ERROR_FILES
            if (errors_dir / name).is_file()
        },
    )


# --------------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------------


def ref_name(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def schema_type(schema: Json) -> str:
    """Render an OpenAPI schema as a compact type string (``int64``, ``PageModel[]`` ...)."""
    if not isinstance(schema, dict) or not schema:
        return "any"
    if "$ref" in schema:
        return ref_name(schema["$ref"])
    for key in ("oneOf", "allOf", "anyOf"):
        if key in schema and len(schema[key]) == 1:
            return schema_type(schema[key][0])
        refs = [part for part in schema.get(key, []) if "$ref" in part]
        if len(refs) == 1:  # NSwag wraps defaulted enums as allOf[{default}, {$ref}]
            return schema_type(refs[0])
    kind = schema.get("type")
    fmt = schema.get("format")
    if kind == "array":
        return schema_type(schema.get("items", {})) + "[]"
    if kind in ("integer", "number"):
        return str(fmt or kind)
    if kind == "string":
        return str(fmt or "string")
    if kind == "boolean":
        return "boolean"
    if kind == "object" or "additionalProperties" in schema:
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict) and extra:
            return f"map<string,{schema_type(extra)}>"
        return "object"
    return str(kind or "any")


def collect_refs(node: Json, out: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                out.add(ref_name(value))
            else:
                collect_refs(value, out)
    elif isinstance(node, list):
        for value in node:
            collect_refs(value, out)


def transitive_refs(roots: Iterable[str], schemas: Mapping[str, Json]) -> set[str]:
    seen: set[str] = set()
    todo = list(roots)
    while todo:
        name = todo.pop()
        if name in seen or name not in schemas:
            continue
        seen.add(name)
        found: set[str] = set()
        collect_refs(schemas[name], found)
        todo.extend(found - seen)
    return seen


def flat_props(schema: Json, schemas: Mapping[str, Json]) -> tuple[dict[str, Json], list[str]]:
    """Properties and required names of an object schema, with ``allOf`` bases merged in."""
    props: dict[str, Json] = {}
    required: list[str] = []
    for part in schema.get("allOf", []):
        target = schemas.get(ref_name(part["$ref"]), {}) if "$ref" in part else part
        more, req = flat_props(target, schemas)
        props.update(more)
        required.extend(req)
    props.update(schema.get("properties", {}))
    required.extend(schema.get("required", []))
    return props, required


def humanize(identifier: str) -> str:
    """``GetMarksForFile`` -> ``Get marks for file`` (generated summaries use names only)."""
    identifier = re.sub(r"V2$", "", identifier)
    words = re.findall(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+|[A-Z]+", identifier)
    if not words:
        return identifier
    text = " ".join(w if w.isupper() and len(w) > 1 else w.lower() for w in words)
    return text[:1].upper() + text[1:]


def dumps(data: Json) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def error_family(code: int) -> str:
    for low, high, family in ERROR_FAMILIES:
        if low <= code <= high:
            return family
    return "other"


def request_path(path: str) -> str:
    return path.split("?", 1)[0]


def op_key(method: str, path: str) -> str:
    return f"{method.upper()} {path}"


# --------------------------------------------------------------------------------------------
# REST: error dictionary, schemas, operations
# --------------------------------------------------------------------------------------------


def error_dictionary(doc: Json) -> dict[int, str]:
    """Native ``ErrorCodes`` as ``{code: name}``; numbers live only in the enum's description."""
    schema = doc["components"]["schemas"]["ErrorCodes"]
    pairs = re.findall(r"^(\d+) (\w+)\b", schema.get("description", ""), re.MULTILINE)
    codes = {int(code): name for code, name in pairs}
    if sorted(codes.values()) != sorted(schema.get("enum", [])):
        raise BuildError("ErrorCodes description and enum disagree; parser needs attention")
    return codes


def schema_record(name: str, schema: Json, schemas: Mapping[str, Json]) -> Json:
    if "enum" in schema:
        names = schema.get("x-enumNames") or schema["enum"]
        return {
            "kind": "enum",
            "values": {str(n): v for n, v in zip(names, schema["enum"], strict=True)},
        }
    if schema.get("type") == "object" or "properties" in schema or "allOf" in schema:
        props, required = flat_props(schema, schemas)
        fields: dict[str, Json] = {}
        for prop, spec in props.items():
            entry: dict[str, Json] = {"type": schema_type(spec), "required": prop in required}
            if spec.get("nullable"):
                entry["nullable"] = True
            fields[prop] = entry
        record: dict[str, Json] = {"kind": "object", "fields": fields}
        bases = [ref_name(p["$ref"]) for p in schema.get("allOf", []) if "$ref" in p]
        if bases:
            record["extends"] = bases
        return record
    return {"kind": "alias", "type": schema_type(schema)}


def merge_by_profile(per_profile: Mapping[str, Json]) -> Json:
    """Merge per-profile schema records, tagging members not present in every profile."""
    present = [p for p in PROFILES if p in per_profile]
    first = per_profile[present[-1]]
    merged: dict[str, Json] = {"kind": first["kind"], "availableIn": present}
    kinds = {per_profile[p]["kind"] for p in present}
    if len(kinds) > 1:
        merged["kindByProfile"] = {p: per_profile[p]["kind"] for p in present}
    for member in ("fields", "values"):
        if not any(member in per_profile[p] for p in present):
            continue
        names: list[str] = []
        for profile in reversed(present):
            for name in per_profile[profile].get(member, {}):
                if name not in names:
                    names.append(name)
        out: dict[str, Json] = {}
        for name in names:
            having = [p for p in present if name in per_profile[p].get(member, {})]
            variants = {json.dumps(per_profile[p][member][name], sort_keys=True) for p in having}
            if member == "fields":
                entry = dict(per_profile[having[-1]][member][name])
                if len(variants) > 1:
                    entry["byProfile"] = {p: per_profile[p][member][name] for p in having}
            else:
                entry = {"value": per_profile[having[-1]][member][name]}
            if having != present:
                entry["availableIn"] = having
            out[name] = entry
        merged[member] = out
    for key in ("extends", "type"):
        if key in first:
            merged[key] = first[key]
    return merged


@dataclass
class RestView:
    """One operation as seen in one profile."""

    method: str
    path: str
    operation_id: str
    tag: str
    params: list[Json]
    request_body: Json | None
    responses: dict[str, Json]
    codes: set[int]
    deprecated: bool
    refs: set[str]
    code_mismatches: list[Json] = field(default_factory=list)


def rest_view(method: str, path: str, op: Json, doc: Json, codes: Mapping[int, str]) -> RestView:
    schemas = doc["components"]["schemas"]
    params: list[Json] = []
    for param in sorted(op.get("parameters", []), key=lambda p: p.get("x-position", 0)):
        spec = param.get("schema", {})
        entry: dict[str, Json] = {
            "name": param["name"],
            "in": param["in"],
            "type": schema_type(spec),
            "required": bool(param.get("required", False)),
        }
        if "default" in spec:
            entry["default"] = spec["default"]
        if spec.get("nullable"):
            entry["nullable"] = True
        params.append(entry)
    body = op.get("requestBody")
    request_body: Json | None = None
    if body:
        content_type, media = sorted(body.get("content", {}).items())[0]
        spec = media.get("schema", {})
        request_body = {
            "contentType": content_type,
            "contentTypes": sorted(body.get("content", {})),
            "type": schema_type(spec),
            "required": bool(body.get("required", False)),
        }
        target = schemas.get(schema_type(spec)) if "$ref" in json.dumps(spec) else None
        if content_type == "application/json" and target is not None and "enum" not in target:
            props, required = flat_props(target, schemas)
            for prop, pspec in props.items():
                entry = {
                    "name": prop,
                    "in": "body",
                    "type": schema_type(pspec),
                    "required": prop in required,
                }
                if pspec.get("nullable"):
                    entry["nullable"] = True
                params.append(entry)
        elif content_type != "multipart/form-data":
            params.append(
                {
                    "name": body.get("x-name", "body"),
                    "in": "body",
                    "type": schema_type(spec),
                    "required": bool(body.get("required", False)),
                    "wholeBody": True,
                }
            )
    responses: dict[str, Json] = {}
    op_codes: set[int] = set()
    mismatches: list[Json] = []
    for status, resp in sorted(op.get("responses", {}).items()):
        content = resp.get("content") or {}
        entry = {}
        if content:
            content_type, media = sorted(content.items())[0]
            entry = {"contentType": content_type, "type": schema_type(media.get("schema", {}))}
        responses[status] = entry
        for code_text, name in re.findall(
            r"\b(\d{1,4})\s+([A-Z][A-Za-z0-9]+)", resp.get("description", "")
        ):
            code = int(code_text)
            if code not in codes:
                mismatches.append({"code": code, "listedAs": name, "problem": "not in dictionary"})
                continue
            op_codes.add(code)
            if codes[code] != name:
                mismatches.append(
                    {"code": code, "listedAs": name, "dictionary": codes[code], "problem": "name"}
                )
    roots: set[str] = set()
    collect_refs(op.get("parameters", []), roots)
    collect_refs(op.get("requestBody", {}), roots)
    collect_refs(op.get("responses", {}), roots)
    refs = transitive_refs(roots - SIGNATURE_IGNORED_SCHEMAS, schemas) - SIGNATURE_IGNORED_SCHEMAS
    return RestView(
        method=method.upper(),
        path=path,
        operation_id=op["operationId"],
        tag=(op.get("tags") or ["Untagged"])[0],
        params=params,
        request_body=request_body,
        responses=responses,
        codes=op_codes,
        deprecated=bool(op.get("deprecated", False)),
        refs=refs,
        code_mismatches=mismatches,
    )


def rest_op_id(surface: str, operation_id: str) -> str:
    controller, _, action = operation_id.partition("_")
    controller = re.sub(r"V2$", "", controller).lower()
    action = action[:1].lower() + action[1:]
    return f"rest.{REST_SURFACES[surface]}.{controller}.{action}"


def rest_safety(method: str, path: str, rules: Json) -> str:
    key = op_key(method, path)
    overrides = rules.get("overrides", {})
    if key in overrides:
        return str(overrides[key])
    if method in ("GET", "HEAD"):
        return "read"
    if method == "DELETE":
        return "destructive"
    last = request_path(path).rstrip("/").rsplit("/", 1)[-1].lower()
    if method == "POST" and last in {s.lower() for s in rules.get("readPostSegments", [])}:
        return "read"
    if last in {s.lower() for s in rules.get("destructiveSegments", [])}:
        return "destructive"
    return "write"


# --------------------------------------------------------------------------------------------
# SOAP: operations and types from the embedded XSD sources
# --------------------------------------------------------------------------------------------


def xsd_source(page: str) -> ET.Element | None:
    match = re.search(r'<table class="i-xml-source".*?<pre>(.*?)</pre>', page, re.S)
    if match is None:
        return None
    text = html.unescape(re.sub(r"<[^>]+>", "", match.group(1)))
    text = text.replace(" xmlns:xs=", f' xmlns:tns="{SOAP_NS}" xmlns:xs=', 1)
    return ET.fromstring(text)


def xs_type(value: str | None) -> str:
    if not value:
        return "inline"
    return value.split(":", 1)[-1]


def documentation(node: ET.Element) -> str:
    doc = node.find(f"{XS}annotation/{XS}documentation")
    return " ".join("".join(doc.itertext()).split()) if doc is not None else ""


def sequence_elements(node: ET.Element) -> list[ET.Element]:
    for path in (
        f"{XS}complexType/{XS}sequence",
        f"{XS}sequence",
        f"{XS}complexContent/{XS}extension/{XS}sequence",
    ):
        seq = node.find(path)
        if seq is not None:
            return seq.findall(f"{XS}element")
    return []


def soap_element(pages: Mapping[str, str], name: str) -> ET.Element:
    page = pages.get(f"{SOAP_PAGE_PREFIX}~e-{name}.html")
    root = xsd_source(page) if page else None
    if root is None:
        raise BuildError(f"SOAP element page missing or unparsable: {name}")
    return root


def soap_types(pages: Mapping[str, str]) -> dict[str, Json]:
    types: dict[str, Json] = {}
    for file_name, page in sorted(pages.items()):
        match = re.fullmatch(re.escape(SOAP_PAGE_PREFIX) + r"~([cs])-(\w+)\.html", file_name)
        if match is None:
            continue
        root = xsd_source(page)
        if root is None:
            raise BuildError(f"SOAP type page without XSD source: {file_name}")
        name = match.group(2)
        if match.group(1) == "s":
            values = [e.get("value", "") for e in root.iter(f"{XS}enumeration")]
            item = root.find(f"{XS}list")
            if item is not None:
                inner = [e.get("value", "") for e in item.iter(f"{XS}enumeration")]
                types[name] = {"kind": "flags", "values": {v: {"value": v} for v in inner}}
            else:
                types[name] = {"kind": "enum", "values": {v: {"value": v} for v in values}}
            continue
        fields: dict[str, Json] = {}
        for element in sequence_elements(root):
            entry: dict[str, Json] = {
                "type": xs_type(element.get("type")),
                "required": element.get("minOccurs", "1") != "0",
            }
            if element.get("maxOccurs", "1") != "1":
                entry["repeated"] = True
            if element.get("nillable") == "true":
                entry["nullable"] = True
            fields[element.get("name", "")] = entry
        record: dict[str, Json] = {"kind": "object", "fields": fields}
        ext = root.find(f"{XS}complexContent/{XS}extension")
        if ext is not None:
            record["extends"] = [xs_type(ext.get("base"))]
        types[name] = record
    return types


def soap_operation_names(index: str) -> list[str]:
    return sorted(set(re.findall(r"irwebservice40\.asmx/(\w+)</A>", index)))


# --------------------------------------------------------------------------------------------
# Report parsing (structure only)
# --------------------------------------------------------------------------------------------


def report_section(report: str, start: str, end: str) -> str:
    try:
        begin = report.index(start)
        return report[begin : report.index(end, begin)]
    except ValueError as exc:
        raise BuildError(f"report section not found: {start!r}") from exc


def report_rest_ops(section: str) -> set[tuple[str, str]]:
    """``(METHOD, path)`` pairs the report lists, with path parameters normalized to ``{}``."""
    found: set[tuple[str, str]] = set()
    for line in section.splitlines():
        last_path: str | None = None
        for method, path, same in re.findall(
            r"`(GET|POST|PUT|DELETE|HEAD|PATCH)(?: (/api/[^`\s]*))?`( same path)?", line
        ):
            if path:
                last_path = path
            elif same and last_path:
                path = last_path
            if path:
                found.add((method, re.sub(r"\{[^}]*\}", "{}", path)))
    return found


def report_soap_areas(section: str) -> tuple[dict[str, str], dict[str, tuple[int, int]]]:
    areas: dict[str, str] = {}
    counts: dict[str, tuple[int, int]] = {}
    current: str | None = None
    for line in section.splitlines():
        heading = re.match(r"^### (.+?) \((\d+)\)\s*$", line)
        if heading:
            current = heading.group(1)
            counts[current] = (int(heading.group(2)), 0)
            continue
        row = re.match(r"^\| \d+ \| (\w+) \|", line)
        if row and current:
            areas[row.group(1)] = current
            declared, seen = counts[current]
            counts[current] = (declared, seen + 1)
    return areas, counts


def report_error_codes(section: str) -> dict[int, str]:
    return {int(c): n for c, n in re.findall(r"\b(\d{1,4}) ([A-Z][A-Za-z0-9]+)", section)}


# --------------------------------------------------------------------------------------------
# Licensing guard: human-facing text must be our own words
# --------------------------------------------------------------------------------------------

SHINGLE_WORDS = 8


def shingles(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {" ".join(words[i : i + SHINGLE_WORDS]) for i in range(len(words) - SHINGLE_WORDS + 1)}


def vendor_prose(inputs: Inputs) -> set[str]:
    """Word shingles of every prose string Vertafore publishes in the vendor snapshots."""
    found: set[str] = set()

    def walk(node: Json) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("description", "summary", "title") and isinstance(value, str):
                    found.update(shingles(value))
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for docs in inputs.oas.values():
        for doc in docs.values():
            walk(doc)
    for page in [inputs.soap_index, *inputs.soap_pages.values()]:
        found.update(shingles(html.unescape(re.sub(r"<[^>]+>", " ", page))))
    return found


def check_own_words(inputs: Inputs) -> None:
    """Fail when an annotation repeats a run of SHINGLE_WORDS words from vendor prose."""
    vendor = vendor_prose(inputs)
    copied: list[str] = []

    def walk(node: Json, where: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{where}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{where}[{i}]")
        elif isinstance(node, str) and shingles(node) & vendor:
            copied.append(f"{where}: {sorted(shingles(node) & vendor)[0]!r}")

    for stem, data in sorted(inputs.annotations.items()):
        walk(data, stem)
    for name, data in sorted(inputs.error_texts.items()):
        walk(data, name)
    if copied:
        raise BuildError("annotation text copies vendor prose: " + "; ".join(copied))


# --------------------------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------------------------


@dataclass
class Context:
    inputs: Inputs
    dictionaries: dict[str, dict[str, dict[int, str]]]  # surface -> profile -> codes
    schemas: dict[str, dict[str, Json]]  # surface -> merged schemas
    raw_schema_records: dict[str, dict[str, dict[str, Json]]]  # surface -> profile -> name -> rec
    views: dict[str, dict[str, dict[str, RestView]]]  # surface -> key -> profile -> view
    key_to_id: dict[str, str]
    operations: dict[str, Json]
    soap_type_map: dict[str, Json]
    review_extra: list[Json] = field(default_factory=list)


def build_rest(ctx: Context) -> None:
    ann = ctx.inputs.annotations.get("rest", {}) or {}
    for surface in REST_SURFACES:
        docs = ctx.inputs.oas[surface]
        ctx.dictionaries[surface] = {p: error_dictionary(docs[p]) for p in PROFILES}
        per_profile: dict[str, dict[str, Json]] = {}
        for profile in PROFILES:
            all_schemas = docs[profile]["components"]["schemas"]
            per_profile[profile] = {
                name: schema_record(name, spec, all_schemas)
                for name, spec in all_schemas.items()
                if name not in SIGNATURE_IGNORED_SCHEMAS
            }
        ctx.raw_schema_records[surface] = per_profile
        names = sorted({n for recs in per_profile.values() for n in recs})
        ctx.schemas[surface] = {
            name: merge_by_profile(
                {p: per_profile[p][name] for p in PROFILES if name in per_profile[p]}
            )
            for name in names
        }
        views: dict[str, dict[str, RestView]] = {}
        for profile in PROFILES:
            doc = docs[profile]
            for path, item in doc["paths"].items():
                for method, op in item.items():
                    if method not in METHODS:
                        continue
                    key = op_key(method, path)
                    view = rest_view(method, path, op, doc, ctx.dictionaries[surface][profile])
                    views.setdefault(key, {})[profile] = view
        ctx.views[surface] = views
        for key, by_profile in sorted(views.items()):
            any_view = next(iter(by_profile.values()))
            op_id = rest_op_id(surface, any_view.operation_id)
            if op_id in ctx.operations:
                raise BuildError(f"duplicate operation id {op_id}")
            if {v.operation_id for v in by_profile.values()} != {any_view.operation_id}:
                raise BuildError(f"operationId differs across profiles for {key}")
            ctx.key_to_id[key] = op_id
            ctx.operations[op_id] = rest_record(ctx, surface, op_id, key, by_profile, ann)


def view_signature(ctx: Context, surface: str, profile: str, view: RestView) -> Json:
    records = ctx.raw_schema_records[surface][profile]
    return {
        "params": view.params,
        "requestBody": view.request_body,
        "responses": view.responses,
        "schemas": {name: records[name] for name in sorted(view.refs) if name in records},
    }


def diff_notes(ctx: Context, surface: str, profile: str, key: str) -> list[str]:
    """Human-readable differences of one operation between the baseline and ``profile``."""
    base = ctx.views[surface][key][BASELINE]
    other = ctx.views[surface][key][profile]
    notes: list[str] = []
    base_params = {(p["name"], p["in"]): p for p in base.params}
    other_params = {(p["name"], p["in"]): p for p in other.params}
    for name, where in sorted(set(other_params) - set(base_params)):
        notes.append(f"+param {name} ({where})")
    for name, where in sorted(set(base_params) - set(other_params)):
        notes.append(f"-param {name} ({where})")
    for pkey in sorted(set(base_params) & set(other_params)):
        if base_params[pkey] != other_params[pkey]:
            notes.append(f"~param {pkey[0]} ({pkey[1]})")
    if base.request_body != other.request_body:
        notes.append("~requestBody")
    if base.responses != other.responses:
        notes.append("~responses")
    base_records = ctx.raw_schema_records[surface][BASELINE]
    other_records = ctx.raw_schema_records[surface][profile]
    for name in sorted(base.refs | other.refs):
        before = base_records.get(name)
        after = other_records.get(name)
        if before == after:
            continue
        if before is None or after is None:
            notes.append(f"schema {name}: {'added' if before is None else 'removed'}")
            continue
        notes.append(f"schema {name}: {schema_change_text(before, after)}")
    return notes


def schema_change_text(before: Json, after: Json) -> str:
    parts: list[str] = []
    for member, label in (("fields", ""), ("values", "value ")):
        old = before.get(member, {})
        new = after.get(member, {})
        parts += [f"+{label}{n}" for n in sorted(set(new) - set(old))]
        parts += [f"-{label}{n}" for n in sorted(set(old) - set(new))]
        parts += [f"~{label}{n}" for n in sorted(set(old) & set(new)) if old[n] != new[n]]
    if before.get("extends") != after.get("extends"):
        parts.append("~extends")
    return " ".join(parts) if parts else "~changed"


def rest_record(
    ctx: Context, surface: str, op_id: str, key: str, by_profile: dict[str, RestView], ann: Json
) -> Json:
    present = [p for p in PROFILES if p in by_profile]
    latest = by_profile[present[-1]]
    params: list[Json] = []
    seen: dict[tuple[str, str], Json] = {}
    for profile in [BASELINE, *[p for p in reversed(PROFILES) if p != BASELINE]]:
        if profile not in by_profile:
            continue
        for param in by_profile[profile].params:
            pkey = (param["name"], param["in"])
            if pkey not in seen:
                seen[pkey] = dict(param)
                params.append(seen[pkey])
    param_availability: dict[str, list[str]] = {}
    for param in params:
        having = [
            p
            for p in present
            if any(
                (x["name"], x["in"]) == (param["name"], param["in"]) for x in by_profile[p].params
            )
        ]
        if having != present:
            param_availability[param["name"]] = having
    deprecated_in = [p for p in present if by_profile[p].deprecated]
    availability: dict[str, str] = {}
    changes: dict[str, list[str]] = {}
    for profile in PROFILES:
        if profile not in by_profile:
            availability[profile] = "absent"
            continue
        status = "available"
        if profile != BASELINE and BASELINE in by_profile:
            base_sig = view_signature(ctx, surface, BASELINE, by_profile[BASELINE])
            sig = view_signature(ctx, surface, profile, by_profile[profile])
            if base_sig != sig:
                status = "changed"
                changes[profile] = diff_notes(ctx, surface, profile, key)
        if profile in deprecated_in:
            status = "deprecated"
        availability[profile] = status
    codes = sorted(set().union(*(v.codes for v in by_profile.values())))
    gotchas: list[str] = []
    if "?" in latest.path:
        marker = latest.path.split("?", 1)[1]
        gotchas.append(
            f"The published path carries a query marker ('?{marker}'): send "
            f"{request_path(latest.path)} with '{marker}' as a query parameter."
        )
    record: dict[str, Json] = {
        "id": op_id,
        "key": key,
        "surface": surface,
        "method": latest.method,
        "path": latest.path,
        "requestPath": request_path(latest.path),
        "area": latest.tag,
        "summary": humanize(latest.operation_id.partition("_")[2]),
        "summarySource": "generated",
        "auth": "anonymous" if key in set(ann.get("anonymous", [])) else "required",
        "safety": rest_safety(latest.method, latest.path, ann.get("safety", {})),
        "params": params,
        "requestBody": latest.request_body,
        "responses": latest.responses,
        "errors": codes,
        "gotchas": gotchas,
        "availability": availability,
        "paramAvailability": param_availability,
        "deprecation": None,
        "source": {"nativeOperationId": latest.operation_id, "profiles": present},
    }
    if changes:
        record["changes"] = changes
    if deprecated_in:
        dep = (ann.get("deprecations") or {}).get(key)
        if dep is None:
            raise BuildError(f"deprecated operation needs an entry in rest.yaml: {key}")
        record["deprecation"] = {
            "in": deprecated_in,
            "replacementKey": dep["replacement"],
            "note": dep["note"],
            "source": "OAS deprecated flag",
        }
    mismatches = [
        {**m, "profile": p} for p, v in sorted(by_profile.items()) for m in v.code_mismatches
    ]
    if mismatches:
        ctx.review_extra.append({"operation": op_id, "codeListIssues": mismatches})
    return record


def build_soap(ctx: Context) -> None:
    ann = ctx.inputs.annotations.get("soap", {}) or {}
    safety: Mapping[str, str] = ann.get("safety", {})
    names = soap_operation_names(ctx.inputs.soap_index)
    section = report_section(ctx.inputs.report, "## 11. SOAP operation catalog", "## 12.")
    areas, _ = report_soap_areas(section)
    ctx.soap_type_map = soap_types(ctx.inputs.soap_pages)
    missing = sorted(set(names) - set(safety))
    extra = sorted(set(safety) - set(names))
    if missing or extra:
        raise BuildError(f"soap.yaml safety mismatch: missing={missing} unknown={extra}")
    for name in names:
        request = soap_element(ctx.inputs.soap_pages, name)
        response = soap_element(ctx.inputs.soap_pages, f"{name}Response")
        args: list[Json] = []
        has_token = False
        for order, element in enumerate(sequence_elements(request)):
            arg_name = element.get("name", "")
            entry: dict[str, Json] = {
                "name": arg_name,
                "in": "body",
                "order": order,
                "type": xs_type(element.get("type")),
                "required": element.get("minOccurs", "1") != "0"
                or "(required)" in documentation(element),
            }
            if element.get("maxOccurs", "1") != "1":
                entry["repeated"] = True
            if arg_name == "securityToken":
                has_token = True
                entry["token"] = True
                entry["required"] = True
            args.append(entry)
        results = [
            {"name": e.get("name", ""), "type": xs_type(e.get("type"))}
            for e in sequence_elements(response)
        ]
        echoes_token = any(r["name"] == "securityToken" for r in results)
        result = next((r for r in results if r["name"] != "securityToken"), None)
        level = safety[name]
        if level not in SAFETY_LEVELS:
            raise BuildError(f"bad safety {level!r} for soap {name}")
        op_id = f"soap.{name}"
        key = f"soap:{name}"
        ctx.key_to_id[key] = op_id
        ctx.operations[op_id] = {
            "id": op_id,
            "key": key,
            "surface": "soap",
            "operation": name,
            "soapAction": f"{SOAP_NS}/{name}",
            "requestElement": name,
            "responseElement": f"{name}Response",
            "area": areas.get(name, "Unclassified"),
            "summary": humanize(name),
            "summarySource": "generated",
            "auth": "required" if has_token else "anonymous",
            "tokenRotation": has_token and echoes_token,
            "safety": level,
            "params": args,
            "result": result,
            "errors": [],
            "gotchas": [],
            "availability": dict.fromkeys(PROFILES, "available"),
            "paramAvailability": {},
            "deprecation": None,
            "source": {"soapActionDerivedFrom": "ASMX document/literal convention"},
        }


# --------------------------------------------------------------------------------------------
# Annotations: operations, flows, capabilities
# --------------------------------------------------------------------------------------------


def resolve_key(ctx: Context, key: str, where: str) -> str:
    if key not in ctx.key_to_id:
        raise BuildError(f"{where}: unknown operation {key!r}")
    return ctx.key_to_id[key]


def type_fields(ctx: Context, surface: str, type_name: str) -> dict[str, Json] | None:
    """Fields of a named type (array suffixes and SOAP ``ArrayOfX`` wrappers unwrapped)."""
    name = type_name.removesuffix("[]")
    if surface == "soap":
        record = ctx.soap_type_map.get(name)
        if record and name.startswith("ArrayOf") and len(record.get("fields", {})) == 1:
            inner = next(iter(record["fields"].values()))["type"]
            return type_fields(ctx, surface, inner)
    else:
        record = ctx.schemas[surface].get(name)
    if not record or record.get("kind") != "object":
        return None
    fields: dict[str, Json] = {}
    if surface == "soap":  # REST records already have allOf bases merged in
        for base in record.get("extends", []):
            fields.update(type_fields(ctx, surface, base) or {})
    fields.update(record["fields"])
    return fields


def field_type(ctx: Context, surface: str, type_name: str, dotted: str, where: str) -> str:
    """Type of ``dotted`` (``Id.RefId``) on ``type_name``; list outputs are indexed implicitly."""
    current = type_name
    for part in dotted.split("."):
        fields = type_fields(ctx, surface, current)
        if fields is None or part not in fields:
            raise BuildError(f"{where}: field {dotted!r} not found on {type_name}")
        current = str(fields[part]["type"])
    return current


def check_field_path(ctx: Context, surface: str, type_name: str, dotted: str, where: str) -> None:
    field_type(ctx, surface, type_name, dotted, where)


def output_type(op: Json) -> str | None:
    if op["surface"] == "soap":
        return op["result"]["type"] if op.get("result") else None
    for status in ("200", "201"):
        resp = op["responses"].get(status)
        if resp and resp.get("type"):
            return str(resp["type"])
    return None


def apply_operation_annotations(ctx: Context) -> None:
    ann: Mapping[str, Json] = ctx.inputs.annotations.get("operations", {}) or {}
    for key, note in sorted(ann.items()):
        op_id = resolve_key(ctx, key, "operations.yaml")
        op = ctx.operations[op_id]
        op["summary"] = note["summary"]
        op["summarySource"] = "annotated"
        op["annotated"] = True
        if "explanation" in note:
            op["explanation"] = note["explanation"]
        if "safety" in note:
            if note["safety"] not in SAFETY_LEVELS:
                raise BuildError(f"operations.yaml: bad safety for {key}")
            op["safety"] = note["safety"]
        op["gotchas"] = [*op["gotchas"], *note.get("gotchas", [])]
        if "returns" in note:
            op["returns"] = note["returns"]
        if "contentType" in note:
            if not op.get("requestBody"):
                raise BuildError(f"operations.yaml: {key} has no request body to retype")
            op["requestBody"]["contentType"] = note["contentType"]
        for part in note.get("multipart", []):
            op["params"].append(
                {
                    "name": part["name"],
                    "in": "multipart",
                    "type": part["type"],
                    "required": bool(part["required"]),
                    "meaning": part["meaning"],
                }
            )
        by_name = {p["name"]: p for p in op["params"]}
        for name, meta in (note.get("params") or {}).items():
            if name not in by_name:
                raise BuildError(f"operations.yaml: {key} has no parameter {name!r}")
            param = by_name[name]
            param["meaning"] = meta["meaning"]
            if "valueFrom" in meta:
                sources: list[Json] = []
                for src in meta["valueFrom"]:
                    target_id = resolve_key(ctx, src["op"], f"{key}.{name}.valueFrom")
                    target = ctx.operations[target_id]
                    out = output_type(target)
                    if src.get("field") and out:
                        check_field_path(
                            ctx, target["surface"], out, src["field"], f"{key}.{name}.valueFrom"
                        )
                    entry = {"op": target_id, "field": src.get("field")}
                    if "note" in src:
                        entry["note"] = src["note"]
                    sources.append(entry)
                param["valueFrom"] = sources
        missing = [
            p["name"]
            for p in op["params"]
            if p.get("required") and "meaning" not in p and not p.get("token")
        ]
        if missing:
            raise BuildError(
                f"operations.yaml: {key} leaves required params unexplained: {missing}"
            )


FLOW_REF = re.compile(r"\$(step(\d+)|input)\.([A-Za-z0-9_.]+)")
INPUT_KEYS = frozenset({"name", "type", "required", "meaning"})


def ref_type(
    ctx: Context,
    ref: str,
    number: int,
    step_types: Mapping[int, tuple[str, str | None]],
    inputs: Mapping[str, Json],
    where: str,
) -> str:
    """Validate one ``$stepN.field`` / ``$input.name`` reference and return its type."""
    match = FLOW_REF.fullmatch(ref)
    if match is None:
        raise BuildError(f"{where}: malformed reference {ref!r}")
    step_no, dotted = match.group(2), match.group(3)
    if step_no is None:
        if dotted not in inputs:
            raise BuildError(f"{where}: unknown flow input in {ref}")
        return str(inputs[dotted]["type"])
    n = int(step_no)
    if n >= number or n not in step_types:
        raise BuildError(f"{where}: {ref} must point to an earlier step")
    surface, out = step_types[n]
    if out is None:
        raise BuildError(f"{where}: {ref} points to a step without output")
    if dotted == "value":
        return out
    return field_type(ctx, surface, out, dotted, where)


def build_flows(ctx: Context) -> Json:
    flows_ann = (ctx.inputs.annotations.get("flows") or {}).get("flows", [])
    flows: dict[str, Json] = {}
    for flow in flows_ann:
        fid = flow["id"]
        if fid in flows:
            raise BuildError(f"flows.yaml: duplicate flow {fid}")
        inputs = {i["name"]: i for i in flow.get("inputs", [])}
        for name, spec in inputs.items():
            if set(spec) - INPUT_KEYS - {"default"} or not set(spec) >= INPUT_KEYS:
                raise BuildError(f"flows.yaml {fid}: input {name} needs {sorted(INPUT_KEYS)}")
        used_inputs: set[str] = set()
        steps_out: list[Json] = []
        step_types: dict[int, tuple[str, str | None]] = {}
        for number, step in enumerate(flow["steps"], start=1):
            where = f"flows.yaml {fid} step {number}"
            op_id = resolve_key(ctx, step["op"], where)
            op = ctx.operations[op_id]
            param_names = {p["name"] for p in op["params"]}
            bound = step.get("params") or {}
            for name in bound:
                if name.split(".", 1)[0] not in param_names:
                    raise BuildError(f"{where}: {op_id} has no parameter {name!r}")
            unbound = [
                p["name"]
                for p in op["params"]
                if p.get("required")
                and not p.get("token")
                and not any(b.split(".", 1)[0] == p["name"] for b in bound)
            ]
            if unbound:
                raise BuildError(f"{where}: required params not bound: {unbound}")
            refs: dict[str, str] = {}
            for match in FLOW_REF.finditer(json.dumps(bound)):
                refs[match.group(0)] = ref_type(
                    ctx, match.group(0), number, step_types, inputs, where
                )
            for match in FLOW_REF.finditer(json.dumps(step)):  # also select/repeat/optional text
                ref_type(ctx, match.group(0), number + 1, step_types, inputs, where)
                if match.group(2) is None:
                    used_inputs.add(match.group(3))
            out = output_type(op)
            step_types[number] = (op["surface"], out)
            record: dict[str, Json] = {
                "n": number,
                "operationId": op_id,
                "safety": op["safety"],
                "purpose": step["purpose"],
                "params": bound,
                "inputsFrom": [{"ref": r, "type": t} for r, t in sorted(refs.items())],
                "output": {"type": out, **({"select": step["select"]} if "select" in step else {})},
            }
            for extra in ("optional", "repeat", "onError"):
                if extra in step:
                    record[extra] = step[extra]
            if "alternatives" in step:
                record["alternatives"] = [
                    resolve_key(ctx, alt, where) for alt in step["alternatives"]
                ]
            steps_out.append(record)
            for linked in [op_id, *record.get("alternatives", [])]:
                ctx.operations[linked].setdefault("flows", [])
                if fid not in ctx.operations[linked]["flows"]:
                    ctx.operations[linked]["flows"].append(fid)
        unused = sorted(set(inputs) - used_inputs)
        if unused:
            raise BuildError(f"flows.yaml {fid}: inputs never referenced: {unused}")
        caveats = list(flow.get("versionCaveats", []))
        for record in steps_out:
            op = ctx.operations[record["operationId"]]
            for profile, status in op["availability"].items():
                if status in ("absent", "deprecated"):
                    caveats.append(f"Step {record['n']} ({op['id']}) is {status} in {profile}.")
        outputs: dict[str, Json] = {}
        for name, ref in (flow.get("outputs") or {}).items():
            out_match = FLOW_REF.fullmatch(ref)
            if out_match is None or out_match.group(2) is None:
                raise BuildError(f"flows.yaml {fid}: output {name} must be a $stepN reference")
            last = len(steps_out) + 1
            outputs[name] = {
                "from": ref,
                "type": ref_type(ctx, ref, last, step_types, inputs, f"flows.yaml {fid}"),
            }
        flows[fid] = {
            "id": fid,
            "title": flow["title"],
            "purpose": flow["purpose"],
            "surface": flow["surface"],
            "inputs": flow.get("inputs", []),
            "steps": steps_out,
            "outputs": outputs,
            "versionCaveats": caveats,
            "notes": flow.get("notes", []),
        }
    for op in ctx.operations.values():
        if "flows" in op:
            op["flows"] = sorted(op["flows"])
    return flows


CAP_PARAM_KEYS = frozenset({"type", "required", "meaning"})


def build_capabilities(ctx: Context) -> Json:
    caps_ann = (ctx.inputs.annotations.get("capabilities") or {}).get("capabilities", {})
    caps: dict[str, Json] = {}
    for cap_id, cap in sorted(caps_ann.items()):
        cap_params: Mapping[str, Json] = cap.get("params") or {}
        for name, spec in cap_params.items():
            if set(spec) != CAP_PARAM_KEYS:
                raise BuildError(f"capabilities.yaml {cap_id}: param {name} needs {CAP_PARAM_KEYS}")
        impls: dict[str, Json] = {}
        levels: set[str] = set()
        for surface, impl in cap["implementations"].items():
            if surface not in SURFACES:
                raise BuildError(f"capabilities.yaml {cap_id}: bad surface {surface}")
            where = f"capabilities.yaml {cap_id}.{surface}"
            op_id = resolve_key(ctx, impl["op"], where)
            op = ctx.operations[op_id]
            if op["surface"] != surface:
                raise BuildError(f"{where}: {op_id} is not on {surface}")
            if op["deprecation"]:
                raise BuildError(f"{where}: capabilities must not route to deprecated {op_id}")
            by_name = {p["name"]: p for p in op["params"]}
            param_map: Mapping[str, str] = impl.get("params") or {}
            fixed: Mapping[str, Json] = impl.get("fixed") or {}
            derived: Mapping[str, str] = impl.get("derived") or {}
            for cap_param in param_map:
                if cap_param not in cap_params:
                    raise BuildError(f"{where}: unknown capability param {cap_param}")
            for native in [*param_map.values(), *fixed, *derived]:
                head, _, rest = native.partition(".")
                if head not in by_name:
                    raise BuildError(f"{where}: {op_id} has no parameter {native!r}")
                if rest:
                    field_type(ctx, surface, str(by_name[head]["type"]), rest, where)
            bound = {n.split(".", 1)[0] for n in [*param_map.values(), *fixed, *derived]}
            unbound = [
                p["name"]
                for p in op["params"]
                if p.get("required") and not p.get("token") and p["name"] not in bound
            ]
            if unbound:
                raise BuildError(f"{where}: required params not bound: {unbound}")
            levels.add(op["safety"])
            impls[surface] = {
                "operationId": op_id,
                "paramMap": dict(param_map),
                **({"fixed": dict(fixed)} if fixed else {}),
                **({"derived": dict(derived)} if derived else {}),
                "verified": False,
                **({"note": impl["note"]} if "note" in impl else {}),
            }
            if op.get("capability", cap_id) != cap_id:
                raise BuildError(f"{where}: {op_id} already implements {op['capability']}")
            op["capability"] = cap_id
        availability: dict[str, str] = {}
        for profile in PROFILES:
            usable = [
                s
                for s, impl in impls.items()
                if ctx.operations[impl["operationId"]]["availability"][profile]
                in ("available", "changed")
            ]
            availability[profile] = "available" if usable else "absent"
        rank = {level: i for i, level in enumerate(SAFETY_LEVELS)}
        caps[cap_id] = {
            "id": cap_id,
            "summary": cap["summary"],
            "params": cap.get("params", {}),
            "safety": max(levels, key=lambda lv: rank[lv]),
            "implementations": impls,
            "availability": availability,
        }
    return caps


# --------------------------------------------------------------------------------------------
# Derived artifacts: matrix, diffs, errors, review
# --------------------------------------------------------------------------------------------


def build_matrix(ctx: Context, caps: Json) -> Json:
    rows = {
        op_id: {
            "availability": op["availability"],
            **({"changes": op["changes"]} if "changes" in op else {}),
            **({"paramAvailability": op["paramAvailability"]} if op["paramAvailability"] else {}),
        }
        for op_id, op in ctx.operations.items()
    }
    deprecations = [
        {
            "operationId": op_id,
            "deprecatedIn": op["deprecation"]["in"],
            "replacement": ctx.key_to_id.get(op["deprecation"]["replacementKey"]),
            "note": op["deprecation"]["note"],
        }
        for op_id, op in sorted(ctx.operations.items())
        if op["deprecation"]
    ]
    for dep in deprecations:
        if dep["replacement"] is None:
            raise BuildError(f"deprecation replacement unknown for {dep['operationId']}")
    return {
        "profiles": list(PROFILES),
        "baseline": BASELINE,
        "legend": {
            "available": "present and identical to the baseline profile",
            "changed": "present, but parameters or referenced schemas differ from the baseline",
            "deprecated": "present and flagged deprecated",
            "absent": "not published for this profile",
        },
        "operations": rows,
        "capabilities": {cid: cap["availability"] for cid, cap in caps.items()},
        "deprecations": deprecations,
    }


def build_version_diff(ctx: Context) -> Json:
    pairs = (("7.2", "24.2"), ("24.2", "25.1"), ("7.2", "25.1"))
    out: dict[str, Json] = {}
    for surface in REST_SURFACES:
        views = ctx.views[surface]
        records = ctx.raw_schema_records[surface]
        dictionary = ctx.dictionaries[surface]
        per_pair: dict[str, Json] = {}
        for old, new in pairs:
            old_keys = {k for k, v in views.items() if old in v}
            new_keys = {k for k, v in views.items() if new in v}
            changed_params: dict[str, Json] = {}
            for key in sorted(old_keys & new_keys):
                a = {(p["name"], p["in"]): p for p in views[key][old].params}
                b = {(p["name"], p["in"]): p for p in views[key][new].params}
                delta = {
                    "added": sorted(f"{n} ({w})" for n, w in set(b) - set(a)),
                    "removed": sorted(f"{n} ({w})" for n, w in set(a) - set(b)),
                    "modified": sorted(
                        f"{n} ({w})" for n, w in set(a) & set(b) if a[(n, w)] != b[(n, w)]
                    ),
                }
                if any(delta.values()):
                    changed_params[ctx.key_to_id[key]] = delta
            changed_schemas: dict[str, str] = {}
            changed_enums: dict[str, str] = {}
            for name in sorted(set(records[old]) & set(records[new])):
                if records[old][name] != records[new][name]:
                    text = schema_change_text(records[old][name], records[new][name])
                    target = (
                        changed_enums if records[new][name]["kind"] == "enum" else changed_schemas
                    )
                    target[name] = text
            per_pair[f"{old}->{new}"] = {
                "addedOperations": sorted(ctx.key_to_id[k] for k in new_keys - old_keys),
                "removedOperations": sorted(ctx.key_to_id[k] for k in old_keys - new_keys),
                "changedParams": changed_params,
                "addedSchemas": sorted(set(records[new]) - set(records[old])),
                "removedSchemas": sorted(set(records[old]) - set(records[new])),
                "changedSchemas": changed_schemas,
                "changedEnums": changed_enums,
                "errorCodes": {
                    "added": sorted(set(dictionary[new]) - set(dictionary[old])),
                    "removed": sorted(set(dictionary[old]) - set(dictionary[new])),
                },
            }
        out[surface] = per_pair
    return out


def build_errors(ctx: Context) -> Json:
    codes: dict[str, Json] = {}
    for surface in REST_SURFACES:
        for profile, dictionary in ctx.dictionaries[surface].items():
            for code, name in dictionary.items():
                entry = codes.setdefault(
                    str(code),
                    {"code": code, "name": name, "family": error_family(code), "profiles": set()},
                )
                if entry["name"] != name:
                    raise BuildError(f"error {code} has different names across profiles")
                entry["profiles"].add(profile)
    raised: dict[str, set[str]] = {}
    for op_id, op in ctx.operations.items():
        for code in op["errors"]:
            raised.setdefault(str(code), set()).add(op_id)
    for key, entry in codes.items():
        entry["profiles"] = [p for p in PROFILES if p in entry["profiles"]]
        entry["raisedBy"] = sorted(raised.get(key, set()))
    v1 = ctx.dictionaries["rest-v1"]
    v2 = ctx.dictionaries["rest-v2"]
    return {
        "note": "Native REST ErrorCodes (v1 and v2). IR codes: data/errors/registry.json.",
        "sameDictionaryInV1AndV2": all(v1[p] == v2[p] for p in PROFILES),
        "rest": codes,
        "soap": {
            "note": "SOAP reports failures as SOAP faults; no numeric dictionary is published.",
        },
    }


def build_native_errors(ctx: Context) -> dict[str, str]:
    """``native-rest.{profile}.json``: each profile's full ErrorCodes dictionary (plan §6.2)."""
    outputs: dict[str, str] = {}
    for profile in PROFILES:
        dictionary = ctx.dictionaries["rest-v1"][profile]
        if dictionary != ctx.dictionaries["rest-v2"][profile]:
            raise BuildError(f"REST v1 and v2 ErrorCodes differ in {profile}")
        codes = {
            str(code): {"name": name, "family": error_family(code)}
            for code, name in sorted(dictionary.items())
        }
        outputs[f"native-rest.{profile}.json"] = dumps(
            {
                "profile": profile,
                "source": f"OAS components.schemas.ErrorCodes ({profile}, v1 and v2 identical)",
                "count": len(codes),
                "codes": codes,
            }
        )
    return outputs


def claim(
    cid: str, section: str, text: str, ok: bool, observed: Json, note: str | None = None
) -> Json:
    entry = {
        "id": cid,
        "reportSection": section,
        "claim": text,
        "status": "confirmed" if ok else "discrepancy",
        "observed": observed,
    }
    if note:
        entry["note"] = note
    return entry


def build_review(ctx: Context, diff: Json) -> Json:
    report = ctx.inputs.report
    ops = ctx.operations
    v1 = diff["rest-v1"]
    v2 = diff["rest-v2"]
    claims: list[Json] = []

    def ids(prefix: str, keys: Iterable[str]) -> list[str]:
        return sorted(ctx.key_to_id[k] for k in keys if k.startswith(prefix))

    email = sorted(i for i in v1["24.2->25.1"]["addedOperations"] if ".emailreceiver." in i)
    added_251 = v1["24.2->25.1"]["addedOperations"]
    claims.append(
        claim(
            "C1",
            "§1, §7",
            "REST v1 25.1 is a superset of 24.2.",
            not v1["24.2->25.1"]["removedOperations"],
            {"removedIn25.1": v1["24.2->25.1"]["removedOperations"]},
        )
    )
    claims.append(
        claim(
            "C2",
            "§1, §7",
            "REST v1 25.1 adds 7 EmailReceiver operations and no other endpoints.",
            len(email) == 7 and len(added_251) == 7,
            {
                "emailReceiverAdded": email,
                "otherAdded": sorted(set(added_251) - set(email)),
                "changedParams": v1["24.2->25.1"]["changedParams"],
                "changedSchemas": v1["24.2->25.1"]["changedSchemas"],
            },
            "The 7 EmailReceiver ops are confirmed. The extra ops and schema changes account for "
            "the unattributed deltas in report §12.2.",
        )
    )
    absent_72 = {
        "GET /api/steps/{stepId}/indexingsettings",
        "GET /api/workflows/rootbuddies",
        "POST /api/steps/users",
    }
    removed_72 = v1["7.2->24.2"]["addedOperations"]
    three = sorted(ctx.key_to_id[k] for k in absent_72)
    claims.append(
        claim(
            "C3",
            "§7",
            "REST v1 7.2 lacks steps/{id}/indexingsettings, workflows/rootbuddies, "
            "POST steps/users.",
            all(ops[i]["availability"]["7.2"] == "absent" for i in three),
            {"operations": three},
        )
    )
    claims.append(
        claim(
            "C4",
            "§1, §7",
            "Those three are the only endpoint-level differences between 7.2 and 24.2 (v1).",
            sorted(removed_72) == three and not v1["7.2->24.2"]["removedOperations"],
            {
                "absentIn7.2": removed_72,
                "extraAbsentIn7.2": sorted(set(removed_72) - set(three)),
                "changedSchemas7.2->24.2": v1["7.2->24.2"]["changedSchemas"],
                "changedEnums7.2->24.2": v1["7.2->24.2"]["changedEnums"],
            },
            "The extra absences account for the unattributed delta in report §12.3.",
        )
    )
    folder_get = ctx.key_to_id["GET /api/v2/folders/{folderId}"]
    claims.append(
        claim(
            "C5",
            "§1, §7",
            "REST v2 25.1 adds the excludeHidden query parameter (GET /api/v2/folders/{folderId}).",
            ops[folder_get]["paramAvailability"].get("excludeHidden") == ["25.1"],
            {"paramAvailability": ops[folder_get]["paramAvailability"]},
        )
    )
    claims.append(
        claim(
            "C6",
            "§7",
            "REST v2 25.1 has no other path changes than excludeHidden.",
            not v2["24.2->25.1"]["addedOperations"] and len(v2["24.2->25.1"]["changedParams"]) == 1,
            {
                "addedOperations": v2["24.2->25.1"]["addedOperations"],
                "changedParams": v2["24.2->25.1"]["changedParams"],
                "changedSchemas": v2["24.2->25.1"]["changedSchemas"],
            },
        )
    )
    tf = ctx.schemas["rest-v2"]["TaskFilterV2"]["fields"]["FileId"]
    claims.append(
        claim(
            "C7",
            "§1, §7",
            "REST v2 7.2 TaskFilterV2 has no FileId.",
            tf.get("availableIn") == ["24.2", "25.1"],
            {
                "TaskFilterV2.FileId.availableIn": tf.get("availableIn"),
                "alsoInV1": ctx.schemas["rest-v1"]["TaskFilterV2"]["fields"]["FileId"].get(
                    "availableIn"
                ),
                "inheritedBy": sorted(
                    n
                    for n, s in ctx.schemas["rest-v2"].items()
                    if "FileId" in s.get("fields", {})
                    and s["fields"]["FileId"].get("availableIn") == ["24.2", "25.1"]
                ),
            },
        )
    )
    age = ctx.schemas["rest-v2"]["TaskAgeCalculationAlgorithm"]["values"]["AvailableDate"]
    claims.append(
        claim(
            "C8",
            "§1, §7",
            "7.2 TaskAgeCalculationAlgorithm lacks AvailableDate.",
            age.get("availableIn") == ["24.2", "25.1"],
            {"AvailableDate.availableIn": age.get("availableIn")},
        )
    )
    dep_counts = {
        surface: {
            p: sorted(
                i
                for i, op in ops.items()
                if op["surface"] == surface and op["availability"][p] == "deprecated"
            )
            for p in PROFILES
        }
        for surface in REST_SURFACES
    }
    claims.append(
        claim(
            "C9",
            "§1, §7",
            "Three deprecations in each v1 profile "
            "(files properties, permissionsbatch, pages delete).",
            all(len(dep_counts["rest-v1"][p]) == 3 for p in PROFILES)
            and not any(dep_counts["rest-v2"].values()),
            {"rest-v1": dep_counts["rest-v1"], "rest-v2": dep_counts["rest-v2"]},
        )
    )
    e917 = ctx.dictionaries["rest-v1"]
    claims.append(
        claim(
            "C10",
            "§7, §8",
            "Error 917 DocFolderInDeletedContent exists only in 25.1.",
            [p for p in PROFILES if 917 in e917[p]] == ["25.1"],
            {"profilesWith917": [p for p in PROFILES if 917 in e917[p]]},
        )
    )
    soap_section = report_section(report, "## 11. SOAP operation catalog", "## 12.")
    areas, counts = report_soap_areas(soap_section)
    soap_ops = sorted(o["operation"] for o in ops.values() if o["surface"] == "soap")
    claims.append(
        claim(
            "C11",
            "§1, §11",
            "The SOAP service has 114 operations; the report's area tables list each once.",
            len(soap_ops) == 114
            and sorted(areas) == soap_ops
            and all(d == s for d, s in counts.values()),
            {
                "published": len(soap_ops),
                "inReportTables": len(areas),
                "missingFromReport": sorted(set(soap_ops) - set(areas)),
                "notPublished": sorted(set(areas) - set(soap_ops)),
                "areaCounts": {a: {"declared": d, "listed": s} for a, (d, s) in counts.items()},
            },
        )
    )
    e2500 = [p for p in PROFILES if 2500 in e917[p]]
    claims.append(
        claim(
            "C12",
            "§12.4",
            "Open item: is error 2500 CanNotRequestBothFileAndTaskLevelHistory in 24.2?",
            True,
            {"profilesWith2500": e2500},
            "Resolved mechanically: 2500 exists only in 7.2.",
        )
    )
    report_codes = report_error_codes(report_section(report, "## 8. REST error catalog", "## 9."))
    base = e917["24.2"]
    report_codes.pop(917, None)
    wrong_names = {
        str(c): {"report": n, "oas": base[c]}
        for c, n in report_codes.items()
        if c in base and base[c] != n
    }
    claims.append(
        claim(
            "C13",
            "§8",
            "The report's error list matches the 24.2 ErrorCodes dictionary.",
            set(report_codes) == set(base) and not wrong_names,
            {
                "inReportNotOAS": sorted(set(report_codes) - set(base)),
                "inOASNotReport": {str(c): base[c] for c in sorted(set(base) - set(report_codes))},
                "nameMismatches": wrong_names,
                "removedIn7.2(unreported)": {
                    str(c): base[c] for c in sorted(set(base) - set(e917["7.2"]))
                },
            },
        )
    )
    for surface, start, end in (
        ("rest-v1", "## 4. REST v1", "## 5. REST v2"),
        ("rest-v2", "## 5. REST v2", "## 6. Key"),
    ):
        listed = report_rest_ops(report_section(report, start, end))
        published: dict[tuple[str, str], str] = {}
        for key in ctx.views[surface]:
            if BASELINE in ctx.views[surface][key]:
                method, path = key.split(" ", 1)
                published[(method, re.sub(r"\{[^}]*\}", "{}", path))] = key
        lower = {(m, p.lower()): k for (m, p), k in published.items()}
        not_published = sorted(
            f"{m} {p}" for m, p in listed if (m, p) not in published and (m, p.lower()) not in lower
        )
        case_only = sorted(
            f"{m} {p}" for m, p in listed if (m, p) not in published and (m, p.lower()) in lower
        )
        unlisted = sorted(
            ctx.key_to_id[k]
            for (m, p), k in published.items()
            if (m, p) not in listed and (m, p.lower()) not in {(a, b.lower()) for a, b in listed}
        )
        claims.append(
            claim(
                "C14" if surface == "rest-v1" else "C15",
                "§4" if surface == "rest-v1" else "§5",
                f"The report's {surface} 24.2 endpoint list is complete and accurate.",
                not not_published and not unlisted,
                {
                    "inReportNotPublished": not_published,
                    "caseDiffersOnly": case_only,
                    "publishedNotInReport": unlisted,
                },
            )
        )
    tags = sorted(
        {v.tag for key in ctx.views["rest-v1"] for v in ctx.views["rest-v1"][key].values()}
    )
    portal_only = [
        "Annotations",
        "Config",
        "Dashboard",
        "DataValidation",
        "Devices",
        "DocumentFilters",
        "Email",
        "Images",
        "Logs",
        "Rruser",
    ]
    present = sorted(
        t for t in portal_only if any(t.lower().rstrip("s") == x.lower().rstrip("s") for x in tags)
    )
    claims.append(
        claim(
            "C16",
            "§9",
            "Portal topics Annotations, Config, Dashboard, DataValidation, Devices, "
            "DocumentFilters, Email, Images, Logs and Rruser do not appear as v1 OAS tags.",
            not present,
            {"presentAsTags": present, "allV1Tags": tags},
        )
    )
    return {
        "note": "Mechanical checks of the research report against the vendor snapshots. "
        "Discrepancies are for human review; the report itself is not modified.",
        "claims": claims,
        "vendorInconsistencies": sorted(ctx.review_extra, key=lambda x: str(x["operation"])),
    }


def review_markdown(review: Json) -> str:
    lines = [
        "# Catalog review: research report vs vendor snapshots",
        "",
        "Generated by `scripts/build_catalog.py`. Do not edit by hand.",
        "",
        "| Claim | Report § | Status | Claim text |",
        "|---|---|---|---|",
    ]
    for c in review["claims"]:
        lines.append(f"| {c['id']} | {c['reportSection']} | {c['status']} | {c['claim']} |")
    lines.append("")
    for c in review["claims"]:
        if c["status"] == "confirmed" and "note" not in c:
            continue
        lines += [f"## {c['id']} ({c['status']}): {c['claim']}", ""]
        if "note" in c:
            lines += [c["note"], ""]
        lines += ["```json", json.dumps(c["observed"], indent=2, sort_keys=True), "```", ""]
    issues = review["vendorInconsistencies"]
    if issues:
        lines += [
            "## Vendor inconsistencies",
            "",
            "Places where an operation's documented error list disagrees with the ErrorCodes "
            "dictionary of the same file.",
            "",
            "```json",
            json.dumps(issues, indent=2, sort_keys=True),
            "```",
            "",
        ]
    return "\n".join(lines)


def build(inputs: Inputs) -> dict[str, dict[str, str]]:
    """Return ``{"catalog"|"errors": {file name: content}}`` for every generated file (no I/O)."""
    ctx = Context(
        inputs=inputs,
        dictionaries={},
        schemas={},
        raw_schema_records={},
        views={},
        key_to_id={},
        operations={},
        soap_type_map={},
    )
    check_own_words(inputs)
    build_rest(ctx)
    build_soap(ctx)
    apply_operation_annotations(ctx)
    flows = build_flows(ctx)
    caps = build_capabilities(ctx)
    for op in ctx.operations.values():
        if op["safety"] not in SAFETY_LEVELS:
            raise BuildError(f"operation without valid safety: {op['id']}")
        if op["deprecation"]:
            op["deprecation"]["replacement"] = ctx.key_to_id[op["deprecation"]["replacementKey"]]
    schemas = {**ctx.schemas, "soap": ctx.soap_type_map}
    diff = build_version_diff(ctx)
    review = build_review(ctx, diff)
    counts = {s: sum(1 for op in ctx.operations.values() if op["surface"] == s) for s in SURFACES}
    header = {"profiles": list(PROFILES), "baseline": BASELINE}
    catalog = {
        "sources.json": dumps({**header, "inputs": inputs.sources}),
        "operations.json": dumps({**header, "counts": counts, "operations": ctx.operations}),
        "schemas.json": dumps({**header, "schemas": schemas}),
        "errors.json": dumps(build_errors(ctx)),
        "matrix.json": dumps(build_matrix(ctx, caps)),
        "version_diff.json": dumps({**header, "diff": diff}),
        "flows.json": dumps({**header, "flows": flows}),
        "capabilities.json": dumps({**header, "capabilities": caps}),
        "review.json": dumps(review),
        "REVIEW.md": review_markdown(review),
    }
    return {"catalog": catalog, "errors": build_native_errors(ctx)}


def generated(group: str, name: str) -> bool:
    """data/errors mixes generated and hand-written files; only the former are managed here."""
    return group == "catalog" or name.startswith("native-rest.")


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------


def default_paths() -> tuple[Path, Path]:
    vendor = os.environ.get("IMAGERIGHT_VENDOR_DIR")
    report = os.environ.get("IMAGERIGHT_REPORT")
    return (
        Path(vendor) if vendor else DEFAULT_VENDOR,
        Path(report) if report else DEFAULT_REPORT,
    )


def write_outputs(outputs: Mapping[str, str], out_dir: Path, group: str = "catalog") -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.iterdir():
        if stale.is_file() and stale.name not in outputs and generated(group, stale.name):
            stale.unlink()
    for name, content in outputs.items():
        (out_dir / name).write_text(content, encoding="utf-8")


def stale_files(outputs: Mapping[str, str], out_dir: Path, group: str = "catalog") -> list[str]:
    stale = [
        name
        for name, content in outputs.items()
        if not (out_dir / name).is_file() or (out_dir / name).read_text(encoding="utf-8") != content
    ]
    if out_dir.is_dir():
        stale += sorted(
            p.name for p in out_dir.iterdir() if p.name not in outputs and generated(group, p.name)
        )
    return sorted(stale)


def main(argv: list[str] | None = None) -> int:
    vendor, report = default_paths()
    parser = argparse.ArgumentParser(description="Build data/catalog from vendor snapshots.")
    parser.add_argument("--vendor", type=Path, default=vendor)
    parser.add_argument("--report", type=Path, default=report)
    parser.add_argument("--annotations", type=Path, default=ANNOTATIONS_DIR)
    parser.add_argument("--out", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--errors-out", type=Path, default=ERRORS_DIR)
    parser.add_argument("--check", action="store_true", help="fail if committed files differ")
    args = parser.parse_args(argv)
    targets = {"catalog": args.out, "errors": args.errors_out}
    try:
        outputs = build(load_inputs(args.vendor, args.report, args.annotations, args.errors_out))
    except BuildError as exc:
        print(f"build_catalog: {exc}", file=sys.stderr)
        return 2
    if args.check:
        stale = [
            f"{group}/{name}"
            for group, files in outputs.items()
            for name in stale_files(files, targets[group], group)
        ]
        if stale:
            print(f"build_catalog: out of date: {', '.join(stale)}", file=sys.stderr)
            return 1
        print("build_catalog: up to date")
        return 0
    for group, files in outputs.items():
        write_outputs(files, targets[group], group)
        print(f"build_catalog: wrote {len(files)} files to {targets[group]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
