"""Client / config tools (plan §3.3; M6): ``ir_call``, ``ir_test_connection``, ``ir_session`` and
``ir_configure``. Thin handlers over the shared ``Runtime`` client; every one returns the standard
envelope, and none accepts credentials.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import Field

from imageright_mcp.catalog import get_catalog
from imageright_mcp.client.health import (
    check_connection,
    session_login,
    session_logout,
    session_status,
)
from imageright_mcp.config import ConfigError
from imageright_mcp.envelope import internal_error, to_envelope
from imageright_mcp.errors import get_registry
from imageright_mcp.runtime import RUNTIME_SETTINGS, ConfigureError, Runtime

SurfaceName = Literal["rest-v2", "rest-v1", "soap"]


def _config_error(exc: ConfigError | ConfigureError) -> CallToolResult:
    if isinstance(exc, ConfigureError):
        return to_envelope(error=exc.error)
    return to_envelope(error=get_registry().error("IR-1001", message=str(exc)))


def register_client_tools(server: MCPServer, runtime: Runtime) -> None:
    @server.tool(
        name="ir_call",
        title="Call an ImageRight operation",
        description=(
            "Execute (or preview) one ImageRight operation. Pass operationId for one exact "
            "native operation (e.g. rest.v1.pages.createPage, soap.GetDocumentByRef), or "
            "capabilityId for a surface-independent action (e.g. document.get, task.create); "
            "a capability is routed to REST v2, REST v1 or SOAP by version and preference, and "
            "meta.route explains the choice. params holds path/query/body fields or SOAP "
            "arguments (for a capability: its own camelCase params, see ir_describe_api). "
            "files maps multipart part names (or a capability's path param) to local files "
            "inside the allowed roots. Writes follow writeMode: previews by default; a "
            "destructive call needs confirm=<previewId> from a prior preview. dryRun=true "
            "always previews and never touches the network. Capability results use canonical "
            "entities (File, Folder, Document, Page, Task, Workflow, Step, User); includeRaw "
            "keeps each upstream object under raw."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    )
    async def ir_call(
        operationId: Annotated[
            str | None, Field(description="Native operation, e.g. rest.v1.tasks.lock.")
        ] = None,
        capabilityId: Annotated[
            str | None, Field(description="Capability, e.g. document.get or page.create.")
        ] = None,
        params: Annotated[
            dict[str, Any] | None,
            Field(description="Arguments: path, query and body fields, or SOAP arguments."),
        ] = None,
        files: Annotated[
            dict[str, str] | None,
            Field(description="Multipart part name -> local file path (allowed roots only)."),
        ] = None,
        surface: Annotated[
            SurfaceName | None,
            Field(description="Force a surface for a capability (overrides the preference)."),
        ] = None,
        dryRun: Annotated[
            bool | None,
            Field(description="true: preview only. false: execute, if writeMode allows it."),
        ] = None,
        includeRaw: Annotated[
            bool, Field(description="Keep the upstream object under raw in canonical entities.")
        ] = False,
        confirm: Annotated[
            str | None,
            Field(description="previewId from a prior dry-run of the identical request."),
        ] = None,
    ) -> CallToolResult:
        registry = get_registry()
        if (operationId is None) == (capabilityId is None):
            code = "IR-3005" if operationId is None else "IR-3006"
            return to_envelope(
                error=registry.error(
                    code, message="Pass exactly one of operationId or capabilityId."
                )
            )
        if capabilityId is not None and capabilityId.strip() not in get_catalog().capabilities:
            error = get_catalog().unknown_operation(capabilityId).to_error()
            return to_envelope(error=error)
        ident = operationId if operationId is not None else capabilityId
        assert ident is not None
        try:
            client = await runtime.client()
            outcome = await client.call(
                ident,
                params,
                files,
                dry_run=dryRun,
                confirm=confirm,
                surface=surface,
                include_raw=includeRaw,
            )
        except (ConfigError, ConfigureError) as exc:
            return _config_error(exc)
        except Exception as exc:
            return to_envelope(error=internal_error(exc))
        return outcome.envelope()

    @server.tool(
        name="ir_test_connection",
        title="Test the ImageRight connection",
        description=(
            "End-to-end health check per configured surface: reachability (REST GET "
            "/api/health, SOAP Version()), authentication, the server-reported version "
            "(GET /api/integration/version) compared with the configured one (IR-1004 on a "
            "mismatch), and latency. In JWT mode the token is also validated; for SOAP without "
            "soapConnection the available connection names are listed. Never returns tokens."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True, idempotent_hint=True, open_world_hint=True
        ),
    )
    async def ir_test_connection(
        surfaces: Annotated[
            list[Literal["rest", "soap"]] | None,
            Field(description="Surfaces to test; defaults to every configured one."),
        ] = None,
    ) -> CallToolResult:
        try:
            client = await runtime.client()
            outcome = await check_connection(client, list(surfaces) if surfaces else None)
        except (ConfigError, ConfigureError) as exc:
            return _config_error(exc)
        except Exception as exc:
            return to_envelope(error=internal_error(exc))
        return outcome.envelope()

    @server.tool(
        name="ir_session",
        title="Inspect or control auth sessions",
        description=(
            "Auth sessions per surface. status: authenticated?, auth mode, token expiry where "
            "known, login and SOAP token-rotation counts. login / refresh: re-authenticate now "
            "with the configured credentials. logout: SOAP UserLogoff; REST drops its token. "
            "Token values are never returned."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    )
    async def ir_session(
        action: Annotated[
            Literal["status", "login", "refresh", "logout"],
            Field(description="status (default), login / refresh, or logout."),
        ] = "status",
        surface: Annotated[
            Literal["rest", "soap", "all"], Field(description="Which session.")
        ] = "all",
    ) -> CallToolResult:
        try:
            client = await runtime.client()
            data: dict[str, Any] = {"action": action}
            if action in {"login", "refresh"}:
                data["result"] = await session_login(client, surface)
            elif action == "logout":
                data["result"] = await session_logout(client, surface)
            status = session_status(client)
            data["sessions"] = status if surface == "all" else {surface: status[surface]}
        except (ConfigError, ConfigureError) as exc:
            return _config_error(exc)
        except Exception as exc:
            return to_envelope(error=internal_error(exc))
        data = client.redactor.value(data)
        failures = [
            r["error"]
            for r in (data.get("result") or {}).values()
            if isinstance(r, dict) and "error" in r
        ]
        if failures:
            error = dict(failures[0])
            error["result"] = data
            return to_envelope(error=error)
        return to_envelope(data=data)

    @server.tool(
        name="ir_configure",
        title="Change settings for this session",
        description=(
            "Session-scoped override of non-secret settings; returns the new effective config "
            "(secrets redacted), which keys changed, and warnings. settings is an object with "
            f"any of: {', '.join(sorted(RUNTIME_SETTINGS))}. reset=true drops all overrides "
            "first. Credentials (password, JWT, private key, SAML token) are never accepted "
            "here: set them in the server's environment. Moving an endpoint to another host "
            "withholds the environment credentials for the session."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    async def ir_configure(
        settings: Annotated[
            dict[str, Any] | None,
            Field(description='Setting name -> new value, e.g. {"writeMode": "allow"}.'),
        ] = None,
        reset: Annotated[bool, Field(description="Drop all session overrides first.")] = False,
    ) -> CallToolResult:
        try:
            config, changed, warnings = runtime.configure(settings or {}, reset=reset)
            await runtime.client()  # apply now: rebuild or update the shared client
        except (ConfigError, ConfigureError) as exc:
            return _config_error(exc)
        except Exception as exc:
            return to_envelope(error=internal_error(exc))
        data = {
            "config": config.redacted(),
            "changed": changed,
            "overrides": sorted(runtime.overrides),
        }
        return to_envelope(data=data, meta={"warnings": warnings})
