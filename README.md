# imageright-mcp

An [MCP](https://modelcontextprotocol.io) server that helps AI coding assistants work correctly against
Vertafore ImageRight on your product version (24.x, 25.x, or 7.2).

> **Status: pre-alpha (M2).** The offline API explorer and version-matrix tools work; the version-aware client,
> custom error catalog, and dry-run previews are not here yet.

## Tools

All tools below work offline: no server, no credentials.

| Tool | What it does |
|---|---|
| `ir_get_config` | Effective configuration, secrets redacted |
| `ir_search_apis` | Plain-language search over REST v1, REST v2 and SOAP operations |
| `ir_list_areas` | Functional areas with operation counts per surface |
| `ir_describe_api` | One operation in detail: params, value sources, body, multipart parts, response, errors, gotchas, versions |
| `ir_describe_type` | A schema or enum, with per-version differences |
| `ir_list_flows` / `ir_describe_flow` | Multi-step recipes (documentation only; nothing is executed) |
| `ir_check_availability` | Is an operation, parameter, field or enum value present in each version? |
| `ir_compare_versions` | Diff two versions |
| `ir_list_deprecations` | Deprecated operations and their replacements |

## Install

```bash
pip install imageright-mcp   # not published yet; for now: pip install -e ".[dev]"
```

## Run

The server speaks MCP over stdio:

```bash
imageright-mcp
```

Claude Code:

```bash
claude mcp add imageright -e IMAGERIGHT_VERSION=24.x -- imageright-mcp
```

## Configuration

Values come from environment variables first, then an optional JSON config file, then built-in defaults.
`ir_get_config` reports the effective value of each setting and where it came from.

| Setting | Env var | Default |
|---|---|---|
| `irVersion` | `IMAGERIGHT_VERSION` | `24.x` |
| `restBaseUrl` | `IMAGERIGHT_REST_BASE_URL` | none |
| `soapUrl` | `IMAGERIGHT_SOAP_URL` | none |
| `authMode` | `IMAGERIGHT_AUTH_MODE` (`password` \| `jwt` \| `saml`) | `password` |
| `surfacePreference` | `IMAGERIGHT_SURFACE_PREFERENCE` (comma-separated) | `rest-v2,rest-v1,soap` |
| `writeMode` | `IMAGERIGHT_WRITE_MODE` (`deny` \| `dry-run` \| `allow`) | `dry-run` |
| `dryRun` | `IMAGERIGHT_DRY_RUN` | `false` |
| `username` | `IMAGERIGHT_USERNAME` | none |
| `password` | `IMAGERIGHT_PASSWORD` (env only) | none |

Set `IMAGERIGHT_CONFIG_FILE` to the path of a JSON file whose keys are the setting names above. The file may
**not** hold secrets. Credentials are read from the environment only, are never accepted as tool arguments,
and are always redacted in output.

## Tools

| Tool | Description |
|---|---|
| `ir_get_config` | Effective configuration with secrets redacted, the catalog profile the version maps to, and the source of each value |

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy && pytest
```

## Trademarks and affiliation

This is an independent, unofficial project. It is not affiliated with, endorsed by, or sponsored by Vertafore, Inc.
"ImageRight" and "Vertafore" are trademarks of Vertafore, Inc., and belong to their respective owner. They are used
here only to describe what this software works with. The API descriptions in this project are written in our own
words, and no Vertafore documentation or OpenAPI files are redistributed.

## License

[MIT](LICENSE)
