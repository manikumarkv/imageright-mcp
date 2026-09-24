"""IR error registry (plan §6.1-6.2): loads ``data/errors/*`` and builds envelope error objects."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

UNMAPPED_NATIVE = "IR-5099"
TEMPLATE_DEFAULTS = {"objectKind": "object", "operationId": "the operation"}
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def render(template: str, values: Mapping[str, str] | None = None) -> str:
    """Fill ``{objectKind}``-style placeholders; unknown ones fall back to neutral wording."""
    merged = {**TEMPLATE_DEFAULTS, **{k: v for k, v in (values or {}).items() if v}}
    return _PLACEHOLDER.sub(lambda m: merged.get(m.group(1), m.group(0)), template)


class Registry:
    def __init__(
        self,
        registry: Mapping[str, Any],
        soap_faults: Mapping[str, Any],
        native: Mapping[str, Mapping[str, Any]],
    ) -> None:
        self.entries: dict[str, dict[str, Any]] = {e["code"]: e for e in registry["codes"]}
        self._by_name = {e["name"].lower(): code for code, e in self.entries.items()}
        self._by_rest: dict[int, str] = {}
        self._by_http: dict[int, list[str]] = {}
        for code, entry in self.entries.items():
            for native_code in entry["mappings"]["rest"]["errorCodes"]:
                self._by_rest[int(native_code)] = code
            for status in entry["mappings"]["http"]:
                self._by_http.setdefault(int(status), []).append(code)
        self.fault_patterns: list[tuple[re.Pattern[str], str]] = [
            (re.compile(p["pattern"], re.IGNORECASE), p["code"]) for p in soap_faults["patterns"]
        ]
        self.result_failures: dict[str, dict[str, Any]] = {
            rule["operationId"]: rule for rule in soap_faults["resultFailures"]
        }
        # profile -> native code -> {name, family}
        self.native: dict[str, dict[int, dict[str, Any]]] = {
            profile: {int(c): v for c, v in data["codes"].items()}
            for profile, data in native.items()
        }
        self._native_by_name: dict[str, int] = {
            info["name"].lower(): code
            for codes in self.native.values()
            for code, info in codes.items()
        }

    # ------------------------------------------------------------------ lookups

    def entry(self, code: str) -> dict[str, Any]:
        return self.entries[code]

    def code_for_name(self, name: str) -> str | None:
        return self._by_name.get(name.lower())

    def for_native_rest(self, code: int) -> str:
        """IR code for a native REST code; unknown codes go to IR-5099, never dropped."""
        return self._by_rest.get(code, UNMAPPED_NATIVE)

    def is_known_native(self, code: int) -> bool:
        return code in self._by_rest

    def for_http(self, status: int) -> list[str]:
        return list(self._by_http.get(status, []))

    def native_info(self, code: int) -> dict[str, Any] | None:
        """Name, family and profiles of a native REST code, or None if no profile has it."""
        profiles = [p for p, codes in self.native.items() if code in codes]
        if not profiles:
            return None
        info = self.native[profiles[-1]][code]
        return {"code": code, "name": info["name"], "family": info["family"], "profiles": profiles}

    def native_code_for_name(self, name: str) -> int | None:
        return self._native_by_name.get(name.lower())

    def match_fault(self, text: str) -> tuple[str, str] | None:
        """First fault pattern (most specific first) matching ``text``: (IR code, pattern)."""
        for pattern, code in self.fault_patterns:
            if pattern.search(text):
                return code, pattern.pattern
        return None

    # ------------------------------------------------------------------ building errors

    def error(
        self,
        code: str,
        *,
        message: str | None = None,
        hint: str | None = None,
        context: Mapping[str, str] | None = None,
        native: Mapping[str, Any] | None = None,
        suggestions: list[str] | None = None,
    ) -> dict[str, Any]:
        """The envelope ``error`` object; text defaults to the registry's templates."""
        entry = self.entries[code]
        error: dict[str, Any] = {
            "code": code,
            "name": entry["name"],
            "category": entry["category"],
            "message": message or render(entry["message"], context),
            "retryable": entry["retryable"],
            "hint": hint or render(entry["hint"], context),
            "native": dict(native) if native is not None else None,
        }
        if suggestions:
            error["suggestions"] = suggestions
        return error

    def warning(self, code: str, message: str | None = None) -> dict[str, str]:
        entry = self.entries[code]
        return {"code": code, "name": entry["name"], "message": message or entry["message"]}


def load_registry(directory: Path | None = None) -> Registry:
    # Imported here: the catalog package itself depends on this module.
    from imageright_mcp.catalog.data import data_dir

    base = directory or data_dir("errors")

    def read(name: str) -> Any:
        with (base / name).open(encoding="utf-8") as fh:
            return json.load(fh)

    loaded = [read(path.name) for path in base.glob("native-rest.*.json")]
    loaded.sort(key=lambda d: tuple(int(part) for part in d["profile"].split(".")))
    native = {data["profile"]: data for data in loaded}
    return Registry(read("registry.json"), read("soap-faults.json"), native)


@cache
def get_registry() -> Registry:
    return load_registry()
