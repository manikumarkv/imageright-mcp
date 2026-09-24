"""``ir_explain_error``: resolve an IR code, native code or name, or SOAP fault text."""

from __future__ import annotations

import difflib
import re
from typing import Any

from imageright_mcp.catalog import Answer, Catalog, CatalogError
from imageright_mcp.errors.mapper import ErrorContext, ErrorMapper, object_kind
from imageright_mcp.errors.registry import UNMAPPED_NATIVE, Registry, render

_IR_CODE = re.compile(r"^IR-?(\d{4})$", re.IGNORECASE)
_HTTP = re.compile(r"^HTTP\s*(\d{3})$", re.IGNORECASE)


class ErrorExplainer:
    def __init__(self, registry: Registry, catalog: Catalog) -> None:
        self.registry = registry
        self.catalog = catalog

    def explain(self, query: str, operation_id: str | None = None) -> Answer:
        text = query.strip()
        if not text:
            raise CatalogError(
                "IR-3005", "MissingRequiredParam", "The query is empty.", self._usage()
            )
        if operation_id:
            resolved = self.catalog.find_operation_id(operation_id)
            if resolved is None:
                raise self.catalog.unknown_operation(operation_id)
            operation_id = resolved
        found = self._resolve(text)
        code, matched_as = found["code"], found["matchedAs"]
        native = found.get("native")
        family = native["family"] if native else None
        values: dict[str, str] = {}
        kind = object_kind(operation_id, family)
        if kind:
            values["objectKind"] = kind
        if operation_id:
            values["operationId"] = operation_id
        data: dict[str, Any] = {
            "query": query,
            "matchedAs": matched_as,
            "error": self._describe(code, values),
        }
        if native:
            data["native"] = native
        for key in ("matchedPattern", "note"):
            if key in found:
                data[key] = found[key]
        also = [c for c in found.get("alsoMatched", []) if c != code]
        if also:
            data["alsoMatched"] = [
                {"code": c, "name": self.registry.entry(c)["name"]} for c in also
            ]
        data["mappings"] = self._mappings(code)
        raised = self._raised_by(code)
        data["raisedBy"] = raised
        data["raisedByCount"] = len(raised)
        return Answer(data=data)

    # ------------------------------------------------------------------ resolution

    def _resolve(self, text: str) -> dict[str, Any]:
        ir = _IR_CODE.match(text)
        if ir:
            code = f"IR-{ir.group(1)}"
            if code in self.registry.entries:
                return {"code": code, "matchedAs": "ir-code"}
            raise CatalogError(
                "IR-3010",
                "UnknownErrorReference",
                f"{code} is not in the registry.",
                self._usage(),
                self._near_codes(code),
            )
        http = _HTTP.match(text)
        if http:
            return self._http(int(http.group(1)))
        if re.fullmatch(r"\d+", text):
            return self._native_number(int(text))
        by_ir_name = self.registry.code_for_name(text)
        native_code = self.registry.native_code_for_name(text)
        if by_ir_name or native_code is not None:
            candidates = []
            if by_ir_name:
                candidates.append(by_ir_name)
            native = self.registry.native_info(native_code) if native_code is not None else None
            if native_code is not None:
                candidates.append(self.registry.for_native_rest(native_code))
            result: dict[str, Any] = {
                "code": candidates[0],
                "matchedAs": "ir-name" if by_ir_name else "native-rest-name",
                "alsoMatched": candidates[1:],
            }
            if native and not by_ir_name:
                result["native"] = native
            elif native_code is not None:
                result["note"] = (
                    f"Also the native REST name of code {native_code}, which maps to "
                    f"{self.registry.for_native_rest(native_code)}."
                )
            return result
        match = self.registry.match_fault(text)
        if match:
            return {"code": match[0], "matchedAs": "soap-fault", "matchedPattern": match[1]}
        if " " not in text:
            raise CatalogError(
                "IR-3010",
                "UnknownErrorReference",
                f"No error code or name matches {text!r}.",
                self._usage(),
                self._near_names(text),
            )
        return {
            "code": "IR-5003",
            "matchedAs": "soap-fault-unmatched",
            "note": "No fault pattern matches this text; at runtime it surfaces as IR-5003 with "
            "the raw fault kept in error.native.",
        }

    def _native_number(self, number: int) -> dict[str, Any]:
        native = self.registry.native_info(number)
        if native is not None:
            return {
                "code": self.registry.for_native_rest(number),
                "matchedAs": "native-rest-code",
                "native": native,
            }
        if f"IR-{number}" in self.registry.entries:
            return {
                "code": f"IR-{number}",
                "matchedAs": "ir-code",
                "note": f"{number} is not a native REST code; read it as IR-{number}.",
            }
        return {
            "code": UNMAPPED_NATIVE,
            "matchedAs": "native-rest-code-unknown",
            "note": f"No profile defines native code {number}; at runtime it surfaces as "
            f"{UNMAPPED_NATIVE} with the code kept in error.native.",
        }

    def _http(self, status: int) -> dict[str, Any]:
        mapper = ErrorMapper(self.registry, self.catalog.ops)
        error = mapper.from_rest(status, None, ErrorContext(session_established=True))
        if status == 202:
            return {
                "code": "IR-5006",
                "matchedAs": "http-status",
                "note": "202 is an error only for operations that can report DataNotReady.",
            }
        if error is None:
            raise CatalogError(
                "IR-3010",
                "UnknownErrorReference",
                f"HTTP {status} is a success status.",
                self._usage(),
            )
        result: dict[str, Any] = {"code": error["code"], "matchedAs": "http-status"}
        if status == 401:
            result["alsoMatched"] = ["IR-2005"]
            result["note"] = (
                "401 maps to IR-2004 when the token had worked before (expired) and to IR-2005 "
                "when it was never accepted."
            )
        return result

    # ------------------------------------------------------------------ output

    def _describe(self, code: str, values: dict[str, str]) -> dict[str, Any]:
        entry = self.registry.entry(code)
        out = {
            "code": code,
            "name": entry["name"],
            "category": entry["category"],
            "retryable": entry["retryable"],
            "message": render(entry["message"], values),
            "hint": render(entry["hint"], values),
        }
        for key in ("severity", "deprecated", "since"):
            if key in entry:
                out[key] = entry[key]
        return out

    def _mappings(self, code: str) -> dict[str, Any]:
        entry = self.registry.entry(code)
        rest = []
        for native_code in entry["mappings"]["rest"]["errorCodes"]:
            info = self.registry.native_info(native_code)
            rest.append(info or {"code": native_code, "name": None, "profiles": []})
        failures = [
            {k: v for k, v in rule.items() if k != "code"}
            for rule in self.registry.result_failures.values()
            if rule["code"] == code
        ]
        return {
            "rest": rest,
            "http": entry["mappings"]["http"],
            "soap": {
                "faultPatterns": entry["mappings"]["soap"]["faultPatterns"],
                "resultFailures": failures,
            },
        }

    def _raised_by(self, code: str) -> list[str]:
        entry = self.registry.entry(code)
        ops: set[str] = set()
        for native_code in entry["mappings"]["rest"]["errorCodes"]:
            ops.update(self.catalog.errors.get(str(native_code), {}).get("raisedBy", []))
        ops.update(
            rule["operationId"]
            for rule in self.registry.result_failures.values()
            if rule["code"] == code
        )
        return sorted(ops)

    # ------------------------------------------------------------------ suggestions

    @staticmethod
    def _usage() -> str:
        return (
            "Pass an IR code (IR-4301), a native REST code (201), an error name "
            "(TaskLockedByAnotherUser), HTTP 403, or the SOAP fault text."
        )

    def _near_codes(self, code: str) -> list[str]:
        family = code[:4]
        return [c for c in self.registry.entries if c.startswith(family)][:8]

    def _near_names(self, text: str) -> list[str]:
        names = [e["name"] for e in self.registry.entries.values()]
        names += sorted(
            {i["name"] for codes in self.registry.native.values() for i in codes.values()}
        )
        lowered = {n.lower(): n for n in names}
        close = difflib.get_close_matches(text.lower(), list(lowered), n=5, cutoff=0.6)
        return [lowered[c] for c in close]
