"""Connection checks and session controls behind ``ir_test_connection`` and ``ir_session``
(plan §3.3, §4.2, §4.4).

Per surface, ``check_connection`` measures reachability, then authentication, then the server's
own version report:

* REST: ``GET /api/health`` (anonymous), login through the AuthManager, and
  ``GET /api/integration/version``, which is compared with the configured version. A different
  catalog profile is IR-1004: a warning, or an error with ``strictVersion``. In JWT mode the token
  is also checked with ``POST /api/jwtTokens/validate``.
* SOAP: ``Version()`` (anonymous; it reports the web-service build, which is not the product
  release, so it is informational), ``AvailableConnections()`` when ``soapConnection`` is not
  set, then ``UserLogin`` (or ``IsLoggedIn`` for a live session).

Token values never appear in results; everything passes through the client's Redactor.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from imageright_mcp.client.auth import AuthError
from imageright_mcp.client.builder import BuildError
from imageright_mcp.client.models import TransportFailure
from imageright_mcp.client.pipeline import CallOutcome
from imageright_mcp.config import resolve_profile
from imageright_mcp.errors import ErrorContext, get_registry

if TYPE_CHECKING:
    from imageright_mcp.client.rest import RestClient

HEALTH_OP = "rest.v1.health.health"
VERSION_OP = "rest.v1.integration.getVersion"
JWT_VALIDATE_OP = "rest.v1.authentication.validateJwtToken"
SOAP_VERSION_OP = "soap.Version"
SOAP_LOGGED_IN_OP = "soap.IsLoggedIn"


def _ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def version_text(report: Any) -> str | None:
    """``{"Major": 24, "Minor": 2, "Build": 115, ...}`` -> ``"24.2.115"``."""
    if not isinstance(report, dict) or report.get("Major") is None:
        return None
    parts = [report.get("Major"), report.get("Minor"), report.get("Build")]
    return ".".join(str(p) for p in parts if p is not None and p >= 0)


def compare_versions(configured_profile: str | None, server_version: str) -> dict[str, Any]:
    server = resolve_profile(server_version)
    return {
        "serverVersion": server_version,
        "serverProfile": server.profile,
        "configuredProfile": configured_profile,
        "match": server.profile is not None and server.profile == configured_profile,
    }


async def _rest(client: RestClient) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """REST checks; returns (surface report, version comparison or None)."""
    report: dict[str, Any] = {"surface": "rest", "reachable": False, "authenticated": False}
    started = time.monotonic()
    ops = client.catalog.ops
    context = ErrorContext(operation_id=HEALTH_OP, session_established=False)
    # /api/health answers with an octet-stream body: read it, never write it to disk.
    health = replace(client.builder.build(ops[HEALTH_OP], {}, {}), expects_scalar=True)
    health = health.with_headers(client.auth.extra_headers())
    try:
        response = await client.transport.send(health)
    except TransportFailure as exc:
        cause: BaseException = TimeoutError(str(exc)) if exc.timeout else exc
        report["error"] = client.mapper.from_transport_error(cause, context)
        report["latencyMs"] = _ms(started)
        return report, None
    report["health"] = {"httpStatus": response.status, "latencyMs": _ms(started)}
    report["reachable"] = True
    if not 200 <= response.status < 300:
        report["error"] = client.mapper.from_rest(
            response.status, response.content, context, "rest-v1"
        )
        report["latencyMs"] = _ms(started)
        return report, None

    step = time.monotonic()
    try:
        headers = await client.auth.headers()
    except AuthError as exc:
        report["auth"] = {"ok": False, "mode": client.config.authMode, "latencyMs": _ms(step)}
        report["error"] = exc.error
        report["latencyMs"] = _ms(started)
        return report, None
    report["authenticated"] = True
    report["auth"] = {
        "ok": True,
        "mode": client.config.authMode,
        "expiresAt": client.auth.status()["expiresAt"],
        "latencyMs": _ms(step),
    }

    if client.config.authMode == "jwt":
        report["jwtValidation"] = await _validate_jwt(client, headers)

    step = time.monotonic()
    comparison: dict[str, Any] | None = None
    request = client.builder.build(ops[VERSION_OP], {}, {})
    try:
        response = await client._send_authenticated(request)
    except (AuthError, TransportFailure) as exc:
        report["version"] = {"ok": False, "latencyMs": _ms(step)}
        report["error"] = (
            exc.error
            if isinstance(exc, AuthError)
            else client.mapper.from_transport_error(exc, ErrorContext(operation_id=VERSION_OP))
        )
        report["latencyMs"] = _ms(started)
        return report, None
    error = client.mapper.from_rest(
        response.status, response.content, ErrorContext(operation_id=VERSION_OP), "rest-v1"
    )
    if error is not None:
        report["version"] = {"ok": False, "latencyMs": _ms(step)}
        report["error"] = error
    else:
        try:
            body = json.loads(response.content or b"null")
        except ValueError:
            body = None
        text = version_text(body)
        if text is None:
            report["version"] = {"ok": False, "raw": body, "latencyMs": _ms(step)}
        else:
            comparison = compare_versions(client.config.profile.profile, text)
            report["version"] = {"ok": True, **comparison, "raw": body, "latencyMs": _ms(step)}
    report["latencyMs"] = _ms(started)
    return report, comparison


async def _validate_jwt(client: RestClient, headers: dict[str, str]) -> dict[str, Any]:
    step = time.monotonic()
    token = headers.get("Authorization", "").partition(" ")[2]
    op = client.catalog.ops[JWT_VALIDATE_OP]
    request = replace(client.builder.build(op, {"token": token}, {}), expects_scalar=True)
    context = ErrorContext(operation_id=JWT_VALIDATE_OP)
    try:
        response = await client.transport.send(request.with_headers(client.auth.extra_headers()))
    except TransportFailure as exc:
        return {"ok": False, "error": client.mapper.from_transport_error(exc, context)}
    error = client.mapper.from_rest(response.status, response.content, context, "rest-v1")
    if error is not None:
        return {"ok": False, "error": error, "latencyMs": _ms(step)}
    text = response.content.decode("utf-8", "replace").strip().strip('"')
    return {"ok": True, "validTo": text or None, "latencyMs": _ms(step)}


async def _soap(client: RestClient) -> dict[str, Any]:
    report: dict[str, Any] = {"surface": "soap", "reachable": False, "authenticated": False}
    started = time.monotonic()
    soap = client.soap
    session = soap.session
    try:
        call = soap.builder.build(client.catalog.ops[SOAP_VERSION_OP], {}, session.settings.url)
    except BuildError as exc:  # pragma: no cover - Version() takes no arguments
        report["error"] = exc.error
        return report
    try:
        exchange = await session.run(call)
    except AuthError as exc:  # pragma: no cover - Version() is anonymous
        report["error"] = exc.error
        return report
    if exchange.response is None:
        report["error"] = exchange.error
        report["latencyMs"] = _ms(started)
        return report
    report["reachable"] = True
    report["version"] = {
        "ok": exchange.error is None,
        "webService": (exchange.result or {}).get("WebService")
        if isinstance(exchange.result, dict)
        else None,
        "raw": exchange.result,
        "latencyMs": _ms(started),
        "note": "SOAP Version() reports the web-service build, not the product release; "
        "it is not compared with irVersion.",
    }
    if exchange.error is not None:
        report["error"] = exchange.error
        report["latencyMs"] = _ms(started)
        return report
    if not session.settings.connection:
        try:
            report["availableConnections"] = await session.available_connections()
        except AuthError as exc:
            report["error"] = exc.error
            report["latencyMs"] = _ms(started)
            return report
    step = time.monotonic()
    try:
        if session.active:
            probe = soap.builder.build(
                client.catalog.ops[SOAP_LOGGED_IN_OP], {}, session.settings.url
            )
            checked = await session.run(probe)  # re-logs in once if the session expired
            if checked.error is not None:
                raise AuthError(checked.error)
        else:
            await session.login()
    except AuthError as exc:
        report["auth"] = {"ok": False, "latencyMs": _ms(step)}
        report["error"] = exc.error
        report["latencyMs"] = _ms(started)
        return report
    report["authenticated"] = True
    report["auth"] = {"ok": True, "connection": session.connection, "latencyMs": _ms(step)}
    report["latencyMs"] = _ms(started)
    return report


async def check_connection(client: RestClient, surfaces: list[str] | None = None) -> CallOutcome:
    registry = get_registry()
    config = client.config
    configured = [s for s, url in (("rest", config.restBaseUrl), ("soap", config.soapUrl)) if url]
    targets = list(dict.fromkeys(surfaces)) if surfaces else configured
    meta: dict[str, Any] = {
        "irVersion": config.irVersion,
        "profile": config.profile.profile,
        "warnings": [],
    }
    if not targets:
        return CallOutcome(
            error=registry.error(
                "IR-1003",
                message="Neither restBaseUrl nor soapUrl is configured, so there is nothing "
                "to test.",
            ),
            meta=meta,
        )
    results: list[dict[str, Any]] = []
    mismatch: dict[str, Any] | None = None
    for target in targets:
        if target not in configured:
            setting = "soapUrl" if target == "soap" else "restBaseUrl"
            results.append(
                {
                    "surface": target,
                    "reachable": False,
                    "authenticated": False,
                    "error": registry.error("IR-1003", message=f"{setting} is not configured."),
                }
            )
        elif target == "rest":
            report, comparison = await _rest(client)
            results.append(report)
            if comparison is not None and not comparison["match"]:
                mismatch = comparison
        else:
            results.append(await _soap(client))
    for report in results:
        report["ok"] = "error" not in report
    data = {
        "healthy": all(r["ok"] for r in results) and mismatch is None,
        "configuredVersion": config.irVersion,
        "profile": config.profile.profile,
        "surfaces": results,
    }
    if mismatch is not None:
        message = (
            f"The server reports {mismatch['serverVersion']} (profile "
            f"{mismatch['serverProfile'] or 'unsupported'}), but irVersion {config.irVersion} "
            f"selects profile {mismatch['configuredProfile']}."
        )
        if config.strictVersion:
            error = registry.error(
                "IR-1004", message=message, native={"surface": "rest-v1", **mismatch}
            )
            error["surfaces"] = results
            return client._finish(CallOutcome(error=error, meta=meta))
        meta["warnings"].append(registry.warning("IR-1004", message))
    return client._finish(CallOutcome(data=data, meta=meta))


# ---------------------------------------------------------------------------- ir_session


def session_status(client: RestClient) -> dict[str, Any]:
    rest = {**client.auth.status(), "configured": bool(client.config.restBaseUrl)}
    soap = {**client.soap.session.status(), "configured": bool(client.config.soapUrl)}
    return {"rest": rest, "soap": soap}


async def session_login(client: RestClient, surface: str) -> dict[str, Any]:
    """Re-authenticate now; the old REST token / SOAP session is dropped first."""
    results: dict[str, Any] = {}
    if surface in {"rest", "all"} and client.config.restBaseUrl:
        client.auth.logout()
        try:
            await client.auth.headers()
            results["rest"] = {"ok": True}
        except AuthError as exc:
            results["rest"] = {"ok": False, "error": exc.error}
    if surface in {"soap", "all"} and client.config.soapUrl:
        await client.soap.session.logoff()
        try:
            await client.soap.session.login()
            results["soap"] = {"ok": True}
        except AuthError as exc:
            results["soap"] = {"ok": False, "error": exc.error}
    return results


async def session_logout(client: RestClient, surface: str) -> dict[str, Any]:
    results: dict[str, Any] = {}
    if surface in {"rest", "all"}:
        # REST has no logout call: the token is dropped and will simply expire server-side.
        client.auth.logout()
        results["rest"] = {"ok": True, "note": "token dropped locally"}
    if surface in {"soap", "all"}:
        was_active = client.soap.session.active
        logged_off = await client.soap.session.logoff()
        results["soap"] = {"ok": logged_off or not was_active, "userLogoff": logged_off}
    return results
