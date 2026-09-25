"""The IrClient (plan §4, §9): routing, transport, auth, validation, write policy, normalizers."""

from imageright_mcp.client.auth import AuthError, AuthManager, AuthSettings
from imageright_mcp.client.builder import BuildError, RequestBuilder
from imageright_mcp.client.models import (
    BinaryRef,
    FilePart,
    JsonPart,
    PreparedRequest,
    RawResponse,
    TransportFailure,
)
from imageright_mcp.client.policy import PreviewLedger, decide, preview_id
from imageright_mcp.client.redact import Redactor
from imageright_mcp.client.rest import CallOutcome, RestClient
from imageright_mcp.client.router import Route, RouteError, Router
from imageright_mcp.client.transport import (
    BodySink,
    MockReply,
    MockTransport,
    RestTransport,
    RetryPolicy,
    Transport,
    send_with_retries,
)
from imageright_mcp.client.validator import Validation, Validator

# The programmatic entry point phase-2 composites build on: ``IrClient(config).call(...)``.
IrClient = RestClient

__all__ = [
    "AuthError",
    "AuthManager",
    "AuthSettings",
    "BinaryRef",
    "BodySink",
    "BuildError",
    "CallOutcome",
    "FilePart",
    "IrClient",
    "JsonPart",
    "MockReply",
    "MockTransport",
    "PreparedRequest",
    "PreviewLedger",
    "RawResponse",
    "Redactor",
    "RequestBuilder",
    "RestClient",
    "RestTransport",
    "RetryPolicy",
    "Route",
    "RouteError",
    "Router",
    "Transport",
    "TransportFailure",
    "Validation",
    "Validator",
    "decide",
    "preview_id",
    "send_with_retries",
]
