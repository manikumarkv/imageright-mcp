"""Shared fixtures for the M4 client tests. Nothing here touches the network."""

from __future__ import annotations

import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from imageright_mcp.client import BodySink, MockReply, MockTransport, RestClient
from imageright_mcp.config import load_config

BASE = "https://ir.example.test/ImageRight"
USER = "svc-imageright"
PASSWORD = "Pa55-w0rd-SECRET-value"
TOKEN = "tok-AAAA-first-access-token"
TOKEN2 = "tok-BBBB-second-access-token"
FAR_FUTURE = "2099-01-01T00:00:00Z"


class FakeClock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


async def no_sleep(_: float) -> None:
    return None


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that opens an IP connection; local pipes and UNIX sockets are fine."""
    real_connect = socket.socket.connect

    def guarded(self: socket.socket, address: Any) -> None:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError(f"test tried to reach the network: {address!r}")
        real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


ClientFactory = Callable[..., tuple[RestClient, MockTransport]]


@pytest.fixture
def make_client(tmp_path: Path) -> ClientFactory:
    """Build a RestClient over a MockTransport. ``env`` entries override the defaults."""

    def factory(
        env: dict[str, str] | None = None,
        *,
        clock: FakeClock | None = None,
        login: bool = True,
        **kwargs: object,
    ) -> tuple[RestClient, MockTransport]:
        full_env = {
            "IMAGERIGHT_REST_BASE_URL": BASE,
            "IMAGERIGHT_USERNAME": USER,
            "IMAGERIGHT_PASSWORD": PASSWORD,
            "IMAGERIGHT_WRITE_MODE": "allow",
            "IMAGERIGHT_FILE_ROOTS": str(tmp_path),
            "IMAGERIGHT_OUTPUT_DIR": str(tmp_path / "out"),
            **(env or {}),
        }
        config = load_config(full_env)
        mock = MockTransport(sink=BodySink(tmp_path / "out"))
        if login and config.authMode == "password":
            mock.add("POST", "/api/authenticate", MockReply(json=TOKEN), sticky=True)
            mock.add("POST", "/api/validto", MockReply(json=FAR_FUTURE), sticky=True)
        client = RestClient(
            config,
            transport=mock,
            clock=clock or FakeClock(),
            sleep=no_sleep,
            **kwargs,  # type: ignore[arg-type]
        )
        return client, mock

    return factory
