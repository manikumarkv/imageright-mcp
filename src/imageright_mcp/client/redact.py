"""Secret redaction for previews, errors and logs (plan §4.4).

Every credential the process knows (configured secrets and tokens obtained at runtime) is
registered with a ``Redactor``; anything that leaves the client passes through it.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable, Mapping
from typing import Any

from imageright_mcp.config import REDACTED

# Header names whose values are credentials whatever they contain.
SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "set-cookie", "proxy-authorization"})
# Shorter strings are not registered: redacting "a" or "1" would shred unrelated output.
MIN_SECRET_LENGTH = 4


class Redactor:
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets: set[str] = set()
        self._lock = threading.Lock()
        self.sensitive_headers: set[str] = set(SENSITIVE_HEADERS)
        for secret in secrets:
            self.add(secret)

    def add(self, secret: str | None) -> None:
        if secret and len(secret) >= MIN_SECRET_LENGTH:
            with self._lock:
                self._secrets.add(secret)

    def discard(self, secret: str | None) -> None:
        """Forget a secret that is no longer live (a rotated-out SOAP token), so the set stays
        small over a long session."""
        if secret:
            with self._lock:
                self._secrets.discard(secret)

    def add_header(self, name: str) -> None:
        self.sensitive_headers.add(name.lower())

    def text(self, value: str) -> str:
        with self._lock:
            secrets = sorted(self._secrets, key=len, reverse=True)
        for secret in secrets:
            if secret in value:
                value = value.replace(secret, REDACTED)
        return value

    def value(self, obj: Any) -> Any:
        """Recursively redact strings inside JSON-like data."""
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, Mapping):
            return {k: self.value(v) for k, v in obj.items()}
        if isinstance(obj, list | tuple):
            return [self.value(v) for v in obj]
        return obj

    def headers(self, headers: Mapping[str, str]) -> dict[str, str]:
        """Keep the auth scheme (``AccessToken ***``) so previews stay informative."""
        out: dict[str, str] = {}
        for name, value in headers.items():
            if name.lower() == "authorization":
                scheme = value.split(" ", 1)[0] if " " in value else ""
                out[name] = f"{scheme} {REDACTED}".strip()
            elif name.lower() in self.sensitive_headers:
                out[name] = REDACTED
            else:
                out[name] = self.text(value)
        return out


class RedactingFilter(logging.Filter):
    """Scrub registered secrets from log records (a second line of defence; nothing should log
    them in the first place)."""

    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self.redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = self.redactor.text(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = None
        return True
