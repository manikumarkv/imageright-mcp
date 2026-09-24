"""Locate and load the generated catalog files (``data/catalog/*.json``, built in M1)."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

CATALOG_FILES = (
    "operations",
    "schemas",
    "matrix",
    "flows",
    "capabilities",
    "errors",
    "version_diff",
)


def catalog_dir() -> Path:
    """The installed wheel ships the files inside the package; a source checkout reads ``data/``."""
    packaged = Path(str(files("imageright_mcp"))) / "_data" / "catalog"
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[3] / "data" / "catalog"


def load_raw(directory: Path | None = None) -> dict[str, dict[str, Any]]:
    base = directory or catalog_dir()
    raw: dict[str, dict[str, Any]] = {}
    for name in CATALOG_FILES:
        with (base / f"{name}.json").open(encoding="utf-8") as fh:
            raw[name] = json.load(fh)
    return raw
