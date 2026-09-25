"""Capability param mapping (plan §4.3): logical capability params -> one surface's native params.

Each implementation in ``capabilities.json`` carries a declarative ``paramMap`` (capability param
-> native param, dotted for nested SOAP arguments such as ``docRef.RefId``), ``fixed`` native
values, and ``derived`` native arguments that need code. The derivations this module can build
are listed in ``DERIVED``; an implementation that needs any other one is rejected by the router
("needs derived argument ...") instead of being sent half-built.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from imageright_mcp.client.models import FileBase64, FilePart
from imageright_mcp.client.validator import Issue, file_part_names

# Derived native argument -> the capability params it consumes.
DERIVED: dict[str, tuple[str, ...]] = {
    # JSON multipart parts: the request builder assembles them from the mapped body fields.
    "PageCreateData": (),
    "PageUpdateData": (),
    # SOAP images travel inline as base64, read from the allowlisted local file.
    "imageList": ("imagePath",),
    "images": ("imagePath",),
}


def _today() -> str:
    return date.today().isoformat()


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


# Values the catalog notes promise for omitted params: (capability, surface) -> param -> value.
DEFAULTS: dict[tuple[str, str], dict[str, Callable[[], str]]] = {
    # "REST requires DocumentDate; the router fills in the current time when it is omitted."
    ("document.create", "rest-v1"): {"documentDate": _today},
    # "SOAP requires a date; the router sends the current time when availableDate is omitted."
    ("task.route", "soap"): {"availableDate": _now},
}

FileResolver = Callable[[str, str], FilePart]


def unsupported_derived(impl: Mapping[str, Any]) -> list[str]:
    return sorted(set(impl.get("derived") or {}) - set(DERIVED))


def unmappable(impl: Mapping[str, Any], supplied: Collection[str]) -> list[str]:
    """Capability params the caller gave that this implementation has no place for."""
    param_map: Mapping[str, str] = impl.get("paramMap") or {}
    consumed = {p for name in impl.get("derived") or {} for p in DERIVED.get(name, ())}
    return sorted(n for n in supplied if n not in param_map and n not in consumed)


@dataclass
class Mapped:
    params: dict[str, Any] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _set_path(target: dict[str, Any], dotted: str, value: Any) -> None:
    *parents, leaf = dotted.split(".")
    node = target
    for part in parents:
        child = node.get(part)
        if not isinstance(child, dict):
            child = node[part] = {}
        node = child
    node[leaf] = value


def _image(file: FilePart) -> dict[str, Any]:
    # Id 0 / ImageType 0 / no rotation: the values a new, unrotated image carries. Unverified
    # against a live server, like every mapping here (capabilities.json "verified": false).
    return {
        "Id": 0,
        "Rotation": 0,
        "ImageType": 0,
        "Extension": file.path.suffix.lstrip(".").lower(),
        "Data": FileBase64(file.path, file.size, file.sha256),
    }


def map_params(
    capability: Mapping[str, Any],
    surface: str,
    impl: Mapping[str, Any],
    op: Mapping[str, Any],
    params: Mapping[str, Any],
    files: Mapping[str, str],
    resolve_file: FileResolver,
) -> Mapped:
    """Translate capability params to the native params and file parts of ``op``.

    Raises ``BuildError`` (from ``resolve_file``) when an image for a SOAP upload is missing or
    outside the allowed roots.
    """
    out = Mapped()
    specs: Mapping[str, Mapping[str, Any]] = capability["params"]
    param_map: Mapping[str, str] = impl.get("paramMap") or {}
    incoming = dict(params)
    for name, path in files.items():
        if specs.get(name, {}).get("type") == "path":
            incoming[name] = path
        else:
            out.issues.append(_unknown(name, [n for n, s in specs.items() if s["type"] == "path"]))
    for name in list(incoming):
        if name not in specs:
            out.issues.append(_unknown(name, list(specs)))
            del incoming[name]
    for name, make in DEFAULTS.get((str(capability["id"]), surface), {}).items():
        if incoming.get(name) is None:
            incoming[name] = make()
            out.notes.append(f"{name} was not given; sent {incoming[name]} (catalog default).")
    for name, spec in specs.items():
        if spec.get("required") and incoming.get(name) is None:
            meaning = spec.get("meaning", "")
            out.issues.append(Issue("IR-3005", name, f"{name} is required. {meaning}".strip()))

    file_parts = set(file_part_names(op))
    for name, value in incoming.items():
        target = param_map.get(name)
        if target is None:
            continue  # consumed by a derivation below (the router rejected anything else)
        if specs[name]["type"] == "path" and target in file_parts:
            out.files[target] = str(value)
        else:
            _set_path(out.params, target, value)
    for target, value in (impl.get("fixed") or {}).items():
        _set_path(out.params, target, value)
    derived = impl.get("derived") or {}
    image_path = incoming.get("imagePath")
    if image_path is not None and ("imageList" in derived or "images" in derived):
        image = _image(resolve_file("imagePath", str(image_path)))
        if "imageList" in derived:
            out.params["imageList"] = {
                "Version": 0,
                "PreRotation": 0,
                "Rotation": 0,
                "Images": [image],
            }
        if "images" in derived:
            out.params["images"] = [image]
    return out


def _unknown(name: str, known: list[str]) -> Issue:
    close = difflib.get_close_matches(name, known, n=3, cutoff=0.6)
    hint = f" Did you mean {', '.join(close)}?" if close else ""
    accepted = ", ".join(sorted(known)) or "none"
    return Issue(
        "IR-3006",
        name,
        f"Unknown capability parameter {name!r}.{hint} Accepted: {accepted}. For native "
        "parameters, call the operationId instead.",
    )
