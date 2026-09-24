"""IrClient building blocks for REST (plan §4, M4): transport, auth, validation, write policy."""

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

__all__ = [
    "AuthError",
    "AuthManager",
    "AuthSettings",
    "BinaryRef",
    "BodySink",
    "BuildError",
    "CallOutcome",
    "FilePart",
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
    "Transport",
    "TransportFailure",
    "Validation",
    "Validator",
    "decide",
    "preview_id",
    "send_with_retries",
]
