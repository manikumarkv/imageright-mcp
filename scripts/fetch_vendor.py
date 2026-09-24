"""Download Vertafore's published ImageRight API references into a local vendor directory.

The files land OUTSIDE the repository (default: ``../imageright-mcp-vendor``) and must never be
committed: our licensing decision is that Vertafore's files are used to build the catalog but are
not redistributed. ``scripts/build_catalog.py`` reads them and emits our own catalog under
``data/catalog``.

Layout produced::

    oas/{7.2,24.2,25.1}/{v1,v2}.json      raw OpenAPI 3 documents (REST v1 and v2)
    soap/index.html                        SOAP method reference (operation list)
    soap/html/*.html                       per-operation and per-type pages (embedding XSD source)
    MANIFEST.json                          sha256 + source URL per file

The portal does not publish the SOAP WSDL as a file; it publishes the WSDL rendered as HTML, with
the XSD source of every request/response element and type embedded in its page. That is what we
vendor.
A live server also serves the WSDL at ``irwebservice40.asmx?WSDL``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

PORTAL = (
    "https://api.apps.vertafore.com/developer-portal/v1/DEVELOPER-PORTAL-WEB-UI/VERTAFORE/entities/"
    "VERTAFORE/docs"
)
REST_BUNDLES = {"24.2": "IR:REST:24.2.115", "25.1": "IR:REST:25.1.340", "7.2": "IR:REST:7.2.1181"}
REST_RESOURCES = {"v1": "Rdb788c25", "v2": "R67d4945"}
SOAP_BUNDLE = "IR:WS:b986cb490b"
SOAP_RESOURCE = "R659cd175"

REPO = Path(__file__).resolve().parent.parent
DEFAULT_VENDOR = REPO.parent / "imageright-mcp-vendor"


def vendor_dir(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.environ.get("IMAGERIGHT_VENDOR_DIR")
    return Path(env) if env else DEFAULT_VENDOR


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "imageright-mcp catalog builder"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data: bytes = response.read()
        return data


def fetch(dest: Path, delay: float = 0.1) -> dict[str, dict[str, str]]:
    """Download every source file into ``dest`` and return the manifest."""
    if dest.resolve().is_relative_to(REPO.resolve()):
        raise SystemExit(f"refusing to vendor into the repository: {dest}")
    manifest: dict[str, dict[str, str]] = {}

    def save(rel: str, url: str) -> bytes:
        data = _get(url)
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        manifest[rel] = {"url": url, "sha256": hashlib.sha256(data).hexdigest()}
        time.sleep(delay)
        return data

    for profile, bundle in REST_BUNDLES.items():
        for api, resource in REST_RESOURCES.items():
            save(f"oas/{profile}/{api}.json", f"{PORTAL}/{bundle}/resource/{resource}/")

    soap_base = f"{PORTAL}/{SOAP_BUNDLE}/resource/{SOAP_RESOURCE}/"
    save("soap/index.html", soap_base)
    webindex = save("soap/html/webindex.html", soap_base + "webindex.html").decode("utf-8-sig")
    pages = sorted(
        {
            href
            for href in re.findall(r'href="([^"]+\.html)"', webindex, re.IGNORECASE)
            if href.startswith(("ImageRight_ws", "http---imageright"))
        }
    )
    for page in pages:
        save(f"soap/html/{page}", soap_base + urllib.parse.quote(page, safe="~-._"))

    (dest / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--vendor", help="destination (default: $IMAGERIGHT_VENDOR_DIR or sibling)")
    args = parser.parse_args(argv)
    dest = vendor_dir(args.vendor)
    manifest = fetch(dest)
    print(f"fetched {len(manifest)} files into {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
