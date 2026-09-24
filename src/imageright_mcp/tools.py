"""Explorer, version-matrix and error tools (plan §3.1, §3.2, §6): thin handlers over services.

Each handler resolves defaults, calls one Catalog method, and returns the standard envelope.
All of them work offline: no server and no credentials.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import Field

from imageright_mcp.catalog import Answer, Catalog, CatalogError, get_catalog
from imageright_mcp.catalog.service import warning
from imageright_mcp.config import ConfigError, load_config
from imageright_mcp.envelope import internal_error, to_envelope
from imageright_mcp.errors import get_registry
from imageright_mcp.errors.explain import ErrorExplainer

SurfaceArg = Literal["rest-v1", "rest-v2", "soap", "any"]
VersionArg = Annotated[
    str | None,
    Field(
        description="ImageRight version: 24.x, 25.x, 7.2 or an exact 24.2 / 25.1. "
        "Defaults to the configured version."
    ),
]

OFFLINE = ToolAnnotations(read_only_hint=True, open_world_hint=False, idempotent_hint=True)


class _Defaults:
    """Configured version and surface preference, falling back to catalog defaults."""

    def __init__(self, env: Mapping[str, str] | None, catalog: Catalog) -> None:
        self.warnings: list[dict[str, str]] = []
        try:
            config = load_config(env)
        except ConfigError as exc:
            self.version = catalog.baseline
            self.preference = ["rest-v2", "rest-v1", "soap"]
            self.warnings.append(
                warning("IR-1001", "ConfigMissing", f"{exc}; using catalog defaults.")
            )
            return
        self.version = config.profile.profile or config.irVersion
        self.preference = list(config.surfacePreference)


def _respond(run: Callable[[], Answer], extra_warnings: list[dict[str, str]]) -> CallToolResult:
    try:
        answer = run()
    except CatalogError as exc:
        return to_envelope(error=exc.to_error(), meta={"warnings": extra_warnings})
    except Exception as exc:
        return to_envelope(error=internal_error(exc), meta={"warnings": extra_warnings})
    meta = {**answer.meta, "warnings": extra_warnings + answer.warnings}
    return to_envelope(data=answer.data, meta=meta)


def register_catalog_tools(server: MCPServer, env: Mapping[str, str] | None = None) -> None:
    def context() -> tuple[Catalog, _Defaults]:
        catalog = get_catalog()
        return catalog, _Defaults(env, catalog)

    def profile_of(
        catalog: Catalog, defaults: _Defaults, version: str | None, warnings: list[dict[str, str]]
    ) -> str:
        profile, notes = catalog.resolve_version(version, defaults.version)
        warnings.extend(notes)
        return profile

    @server.tool(
        name="ir_search_apis",
        title="Search ImageRight operations",
        description=(
            "Find ImageRight API operations from a plain-language description of the goal, "
            "e.g. 'upload a page', 'assign task to user', 'why is my task stuck in error'. "
            "Covers REST v1, REST v2 and SOAP. Returns ranked operationIds with method and path "
            "(or SOAP operation), summary, area, availability in the chosen version and a "
            "relative score. Deprecated operations are hidden unless includeDeprecated is true. "
            "Follow up with ir_describe_api for details."
        ),
        annotations=OFFLINE,
    )
    def ir_search_apis(
        query: Annotated[str, Field(description="What you want to do, in plain words.")],
        version: VersionArg = None,
        surface: SurfaceArg = "any",
        area: Annotated[
            str | None, Field(description="Restrict to one area (see ir_list_areas).")
        ] = None,
        includeDeprecated: bool = False,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> CallToolResult:
        catalog, defaults = context()
        warnings = list(defaults.warnings)

        def run() -> Answer:
            profile = profile_of(catalog, defaults, version, warnings)
            return catalog.search(
                query,
                profile,
                surface=catalog.resolve_surface(surface),
                area=catalog.resolve_area(area),
                include_deprecated=includeDeprecated,
                limit=limit,
            )

        return _respond(run, warnings)

    @server.tool(
        name="ir_list_areas",
        title="List functional areas",
        description=(
            "Browse the functional areas of the ImageRight APIs (Documents, Pages, Tasks, "
            "Workflow, ...) with the number of operations per surface in the chosen version and "
            "a few representative operations for each."
        ),
        annotations=OFFLINE,
    )
    def ir_list_areas(version: VersionArg = None, surface: SurfaceArg = "any") -> CallToolResult:
        catalog, defaults = context()
        warnings = list(defaults.warnings)

        def run() -> Answer:
            profile = profile_of(catalog, defaults, version, warnings)
            return catalog.list_areas(profile, catalog.resolve_surface(surface))

        return _respond(run, warnings)

    @server.tool(
        name="ir_describe_api",
        title="Describe an operation",
        description=(
            "Explain one ImageRight operation in plain English: method and path (or SOAP "
            "signature), auth, each parameter with its meaning, allowed values and which "
            "operation supplies its value, request body, multipart part names, response model, "
            "possible native errors, gotchas, availability per version, deprecation and "
            "replacement, related operations, flows it belongs to, and how confident the "
            "catalog is. Accepts an operationId (e.g. rest.v1.pages.createPage), a "
            "'METHOD /path' key, a SOAP operation name, or a capabilityId (e.g. page.create), "
            "which resolves to the preferred surface."
        ),
        annotations=OFFLINE,
    )
    def ir_describe_api(
        operationId: Annotated[
            str | None, Field(description="operationId, 'METHOD /path' or SOAP name.")
        ] = None,
        capabilityId: Annotated[
            str | None, Field(description="Surface-independent id, e.g. task.create.")
        ] = None,
        version: VersionArg = None,
    ) -> CallToolResult:
        catalog, defaults = context()
        warnings = list(defaults.warnings)

        def run() -> Answer:
            ident = operationId or capabilityId
            if not ident:
                raise CatalogError(
                    "IR-3005",
                    "MissingRequiredParam",
                    "Pass operationId or capabilityId.",
                    "Use ir_search_apis to find one.",
                )
            profile = profile_of(catalog, defaults, version, warnings)
            return catalog.describe_api(ident, profile, defaults.preference)

        return _respond(run, warnings)

    @server.tool(
        name="ir_describe_type",
        title="Describe a schema or enum",
        description=(
            "Explain a request/response schema or an enum used by the ImageRight APIs: fields "
            "with type, whether required, and meaning where known; enum values; differences "
            "between versions (e.g. TaskFilterV2.FileId is missing in 7.2); and operations that "
            "use it."
        ),
        annotations=OFFLINE,
    )
    def ir_describe_type(
        name: Annotated[str, Field(description="Type name, e.g. TaskCreateModel.")],
        version: VersionArg = None,
        surface: SurfaceArg = "any",
    ) -> CallToolResult:
        catalog, defaults = context()
        warnings = list(defaults.warnings)

        def run() -> Answer:
            profile = profile_of(catalog, defaults, version, warnings)
            return catalog.describe_type(name, profile, catalog.resolve_surface(surface))

        return _respond(run, warnings)

    @server.tool(
        name="ir_list_flows",
        title="List multi-step recipes",
        description=(
            "List the documented multi-step recipes (flows), such as creating a task from "
            "names or ingesting a document page by page, with the surfaces they use and whether "
            "every step exists in the chosen version."
        ),
        annotations=OFFLINE,
    )
    def ir_list_flows(version: VersionArg = None, surface: SurfaceArg = "any") -> CallToolResult:
        catalog, defaults = context()
        warnings = list(defaults.warnings)

        def run() -> Answer:
            profile = profile_of(catalog, defaults, version, warnings)
            return catalog.list_flows(profile, catalog.resolve_surface(surface))

        return _respond(run, warnings)

    @server.tool(
        name="ir_describe_flow",
        title="Describe a recipe step by step",
        description=(
            "Show a flow as ordered calls: which operation to call at each step, what it is "
            "for, which earlier outputs feed it ($stepN.field), required parameters, where it "
            "can fail, and caveats for the chosen version. Documentation only: nothing is "
            "executed."
        ),
        annotations=OFFLINE,
    )
    def ir_describe_flow(
        flowId: Annotated[str, Field(description="Flow id from ir_list_flows, e.g. F3.")],
        version: VersionArg = None,
        surface: SurfaceArg = "any",
    ) -> CallToolResult:
        catalog, defaults = context()
        warnings = list(defaults.warnings)

        def run() -> Answer:
            profile = profile_of(catalog, defaults, version, warnings)
            return catalog.describe_flow(flowId, profile, catalog.resolve_surface(surface))

        return _respond(run, warnings)

    @server.tool(
        name="ir_check_availability",
        title="Check availability per version",
        description=(
            "Check whether an operation, capability, parameter, schema field or enum value "
            "exists in each ImageRight version. Identify the operation by operationId, "
            "capabilityId, or path (+ method). param may be a parameter of that operation or "
            "Type.Member (e.g. TaskAgeCalculationAlgorithm.AvailableDate), which also works on "
            "its own. Each row is available, absent, deprecated or changed, with notes and the "
            "replacement where one exists."
        ),
        annotations=OFFLINE,
    )
    def ir_check_availability(
        operationId: str | None = None,
        capabilityId: str | None = None,
        path: Annotated[str | None, Field(description="REST path, templated or concrete.")] = None,
        method: str | None = None,
        param: str | None = None,
        versions: Annotated[
            list[str] | None, Field(description="Versions to check; default all profiles.")
        ] = None,
    ) -> CallToolResult:
        catalog, defaults = context()
        warnings = list(defaults.warnings)

        def run() -> Answer:
            profiles = (
                list(dict.fromkeys(profile_of(catalog, defaults, v, warnings) for v in versions))
                if versions
                else list(catalog.profiles)
            )
            return catalog.check_availability(
                profiles,
                ident=operationId or capabilityId,
                path=path,
                method=method,
                param=param,
                preference=defaults.preference,
            )

        return _respond(run, warnings)

    @server.tool(
        name="ir_compare_versions",
        title="Compare two versions",
        description=(
            "Diff two ImageRight versions: operations added or removed, changed parameters, "
            "changed schemas and enums, and error codes added or removed. Works in either "
            "direction (e.g. from 24.x to 25.x, or back)."
        ),
        annotations=OFFLINE,
    )
    def ir_compare_versions(
        fromVersion: Annotated[str, Field(description="Version to diff from, e.g. 24.x.")],
        toVersion: Annotated[str, Field(description="Version to diff to, e.g. 25.x.")],
        surface: SurfaceArg = "any",
        area: str | None = None,
    ) -> CallToolResult:
        catalog, defaults = context()
        warnings = list(defaults.warnings)

        def run() -> Answer:
            source = profile_of(catalog, defaults, fromVersion, warnings)
            target = profile_of(catalog, defaults, toVersion, warnings)
            return catalog.compare_versions(
                source, target, catalog.resolve_surface(surface), catalog.resolve_area(area)
            )

        return _respond(run, warnings)

    @server.tool(
        name="ir_list_deprecations",
        title="List deprecated operations",
        description=(
            "List every deprecated ImageRight operation with the versions it is deprecated in, "
            "its replacement and a short note on how to switch."
        ),
        annotations=OFFLINE,
    )
    def ir_list_deprecations(version: VersionArg = None) -> CallToolResult:
        catalog, defaults = context()
        warnings = list(defaults.warnings)

        def run() -> Answer:
            profile = profile_of(catalog, defaults, version, warnings) if version else None
            return catalog.list_deprecations(profile)

        return _respond(run, warnings)


def register_error_tools(server: MCPServer) -> None:
    @server.tool(
        name="ir_explain_error",
        title="Explain an error code",
        description=(
            "Explain an ImageRight error. Accepts an IR code from an envelope (IR-4301), a native "
            "REST error code (201) or name (TaskLockedByAnotherUser), an IR name "
            "(LockedByAnotherUser), an HTTP status (HTTP 403), or SOAP fault text. Returns the "
            "IR code with category, whether a retry can help and what to do next, every native "
            "REST code, HTTP status and SOAP fault pattern that maps to it, and the operations "
            "that can raise it. Pass operationId to tailor the hint to one operation."
        ),
        annotations=OFFLINE,
    )
    def ir_explain_error(
        query: Annotated[
            str, Field(description="IR code, native code or name, HTTP status, or fault text.")
        ],
        operationId: Annotated[
            str | None, Field(description="Operation the error came from, for the hint.")
        ] = None,
    ) -> CallToolResult:
        def run() -> Answer:
            return ErrorExplainer(get_registry(), get_catalog()).explain(query, operationId)

        return _respond(run, [])
