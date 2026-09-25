"""Router and capability layer (plan §4.2, §4.3; M6): pipeline step 2.

An explicit ``operationId`` runs on its own surface. A ``capabilityId`` is routed: each
implementation is checked against the profile, the enabled surfaces, the surface preference and
the capability mapping, and the first one that passes is chosen. Every surface is recorded in the
trail with the reason it was chosen or rejected; the trail goes into ``meta.route`` and into
dry-run previews, so the caller can see *why* a request went to v1 rather than v2.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any

from imageright_mcp.catalog import Catalog
from imageright_mcp.client.capability import unmappable, unsupported_derived
from imageright_mcp.errors import get_registry

SURFACES = ("rest-v2", "rest-v1", "soap")


@dataclass
class Route:
    operation_id: str
    capability_id: str | None
    surface: str
    trail: list[dict[str, Any]]
    # The capability implementation (paramMap, fixed, derived) when routed by capability.
    implementation: dict[str, Any] | None = None
    warnings: list[dict[str, str]] = field(default_factory=list)


class RouteError(Exception):
    def __init__(self, error: dict[str, Any], trail: list[dict[str, Any]] | None = None) -> None:
        super().__init__(error["message"])
        self.error = error
        self.trail = trail or []


class Router:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog

    def route(
        self,
        ident: str,
        *,
        profile: str,
        preference: list[str],
        force: str | None = None,
        enabled: Collection[str] | None = None,
        require_verified: bool = False,
        supplied: Collection[str] = (),
    ) -> Route:
        """Resolve an operationId or capabilityId to one operation.

        ``enabled`` lists the surfaces with a configured endpoint; ``None`` means none is
        configured, so routing goes by catalog and preference only (dry-run still works).
        ``supplied`` names the capability params the caller passed; a surface that cannot
        express one of them is rejected rather than silently dropping it.
        """
        registry = get_registry()
        if force is not None and force not in SURFACES:
            raise RouteError(
                registry.error(
                    "IR-3006",
                    message=f"Unknown surface {force!r}; use one of {', '.join(SURFACES)}.",
                )
            )
        op_id = self.catalog.find_operation_id(ident)
        if op_id is not None:
            return self._explicit(op_id, force)
        capability = self.catalog.capabilities.get(ident.strip())
        if capability is None:
            raise RouteError(self.catalog.unknown_operation(ident).to_error())
        return self._capability(
            capability, profile, preference, force, enabled, require_verified, supplied
        )

    def _explicit(self, op_id: str, force: str | None) -> Route:
        op = self.catalog.ops[op_id]
        surface = str(op["surface"])
        if force is not None and force != surface:
            raise RouteError(
                get_registry().error(
                    "IR-3006",
                    message=f"{op_id} is a {surface} operation, so surface={force} cannot apply.",
                    hint="Drop surface, or pass a capabilityId and let the router pick "
                    "the surface.",
                )
            )
        trail = [{"surface": surface, "chosen": True, "reason": "explicit operationId"}]
        return Route(op_id, op.get("capability"), surface, trail)

    def _capability(
        self,
        capability: Mapping[str, Any],
        profile: str,
        preference: list[str],
        force: str | None,
        enabled: Collection[str] | None,
        require_verified: bool,
        supplied: Collection[str],
    ) -> Route:
        registry = get_registry()
        impls: Mapping[str, dict[str, Any]] = capability["implementations"]
        order = [force] if force else [s for s in preference if s in SURFACES]
        order += [s for s in SURFACES if s not in order]
        trail: list[dict[str, Any]] = []
        chosen: Route | None = None
        for surface in order:
            impl = impls.get(surface)
            entry: dict[str, Any] = {"surface": surface}
            if impl is None:
                trail.append({**entry, "rejected": "no implementation of this capability"})
                continue
            entry["operationId"] = impl["operationId"]
            entry["verified"] = bool(impl.get("verified"))
            reason, warnings = self._reject_reason(
                impl, surface, profile, preference, force, enabled, require_verified, supplied
            )
            if reason is None and chosen is not None:
                reason = f"lower preference than {chosen.surface}"
            if reason is not None:
                trail.append({**entry, "rejected": reason})
                continue
            entry["chosen"] = True
            if force:
                entry["reason"] = "surface forced by the caller"
            if impl.get("note"):
                entry["note"] = impl["note"]
            trail.append(entry)
            chosen = Route(
                str(impl["operationId"]),
                str(capability["id"]),
                surface,
                trail,
                implementation=impl,
                warnings=warnings,
            )
        if chosen is None:
            error = registry.error(
                "IR-3004",
                message=f"No surface can serve {capability['id']} in {profile}"
                + (f" with surface={force}." if force else "."),
                hint="See route for why each surface was rejected. Check ir_check_availability, "
                "ir_get_config (surfacePreference and endpoints), or force a surface.",
            )
            error["route"] = trail
            raise RouteError(error, trail)
        return chosen

    def _reject_reason(
        self,
        impl: Mapping[str, Any],
        surface: str,
        profile: str,
        preference: list[str],
        force: str | None,
        enabled: Collection[str] | None,
        require_verified: bool,
        supplied: Collection[str],
    ) -> tuple[str | None, list[dict[str, str]]]:
        op = self.catalog.ops[str(impl["operationId"])]
        if force is not None and surface != force:
            return f"surface forced to {force}", []
        if force is None and surface not in preference:
            return "not in surfacePreference", []
        status = op["availability"].get(profile, "absent")
        if status == "absent":
            present = sorted(p for p, v in op["availability"].items() if v != "absent")
            return f"absent in {profile} (available in {', '.join(present) or 'none'})", []
        warnings: list[dict[str, str]] = []
        deprecation = op.get("deprecation")
        if deprecation and profile in deprecation.get("in", []):
            text = f"deprecated in {profile}; replacement {deprecation.get('replacement')}"
            if force is None:
                return text, []
            warnings.append(get_registry().warning("IR-3003", f"{op['id']} is {text}."))
        if force is None and enabled is not None and surface not in enabled:
            setting = "soapUrl" if surface == "soap" else "restBaseUrl"
            return f"surface disabled: {setting} is not configured", []
        if force is None and require_verified and not impl.get("verified"):
            return "param mapping not verified against a fixture (requireVerifiedMappings)", []
        missing = unsupported_derived(impl)
        if missing:
            return (
                f"needs derived argument(s) {', '.join(missing)} that this server cannot build yet",
                [],
            )
        lost = unmappable(impl, supplied)
        if lost:
            return f"cannot express parameter(s) {', '.join(lost)} on this surface", []
        return None, warnings
