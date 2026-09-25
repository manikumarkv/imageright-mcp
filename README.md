# imageright-mcp

An [MCP](https://modelcontextprotocol.io) server that helps AI coding assistants work correctly against
Vertafore ImageRight on your product version (24.x, 25.x, or 7.2).

> **Status: pre-alpha (phase 2 complete: 24 tools — 15 API explorer + 9 composite workflow tools; 802
> tests green).** The offline API explorer, version-matrix tools, error catalog, the version-aware client
> (`ir_call`, with dry-run previews) and the composite workflow tools work. Capability param mappings are
> not yet verified against a live server; hardening and a live smoke suite follow in M7.

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
| `ir_explain_error` | Explain an IR code, native REST code or name, HTTP status, or SOAP fault text |

These talk to your server (or preview what they would send):

| Tool | What it does |
|---|---|
| `ir_call` | Execute or preview one `operationId`, or a `capabilityId` routed to REST v2, REST v1 or SOAP by version and preference (`meta.route` says why). Capability results use canonical File / Folder / Document / Page / Task / Workflow / Step / User shapes |
| `ir_test_connection` | Reachability, authentication, server-reported version vs. configured version (IR-1004), latency |
| `ir_session` | Auth session status, re-login, logout (SOAP `UserLogoff`) |
| `ir_configure` | Session-scoped override of non-secret settings. Secrets are refused; moving an endpoint to another host withholds the environment credentials |

Composite tools run a whole flow from `annotations/flows.yaml` using names, codes and file numbers.
Lookups run for real. Writes follow `writeMode`, and a preview lists every planned step. When a flow
has to ask the user something, the tool returns `data.status: "needs-input"` with the question in
`data.needsInput`.

| Tool | Flow |
|---|---|
| `ir_create_task` | F1: create a workflow task on a file, or on one document in a named folder |
| `ir_search_files` | F9: search files by number, `%` pattern, drawer, temporary / deleted state |
| `ir_create_file` | F10: create a file in a drawer, with duplicate-number protection |
| `ir_update_file` | F11: change a file's number and/or description, with duplicate protection |
| `ir_merge_files` | F12: merge one file into another (destructive, so it needs confirmation) |
| `ir_move_file_content` | F13: move or copy documents, optionally filtered by type code, into a folder of another file |
| `ir_find_documents` | F14: list a file's documents by folder, type code and description substring |
| `ir_create_document` | F15: create a document in a folder, optionally creating the file and folder first |
| `ir_upload_document` | F16: split a local PDF into page images and upload them as a new document |

## Errors

Every tool returns the same envelope: `{ok, data, error, meta}`. Failures carry a stable
`IR-CNNN` code (`data/errors/registry.json`) that says what to do next: about 230 native ImageRight error
codes collapse into 75 IR codes, and the native detail is kept in `error.native`. Codes are append-only:
never renumbered, renamed or reused. Arguments that fail the tool's input schema are rejected by the MCP SDK
before the handler runs, so they come back as a plain-text error without the envelope.

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
| `jwt` | `IMAGERIGHT_JWT` (env only): a static JWT | none |
| `jwtPrivateKey` | `IMAGERIGHT_JWT_PRIVATE_KEY` (env only): PEM RSA key for self-signed RS256 JWTs | none |
| `jwtPrivateKeyFile` | `IMAGERIGHT_JWT_PRIVATE_KEY_FILE`: path to that key instead | none |
| `jwtSubject` / `jwtIssuer` / `jwtAudience` | `IMAGERIGHT_JWT_SUBJECT` / `_ISSUER` / `_AUDIENCE` (subject defaults to `username`) | none |
| `jwtTtlSeconds` | `IMAGERIGHT_JWT_TTL_SECONDS` | `300` |
| `samlToken` | `IMAGERIGHT_SAML_TOKEN` (env only): base64 SAML token | none |
| `samlTokenCommand` | `IMAGERIGHT_SAML_TOKEN_COMMAND`: command (no shell) that prints a fresh token | none |
| `extraHeaders` | `IMAGERIGHT_EXTRA_HEADERS` (`Header=ENV_VAR,...`): header -> env var holding its value | none |
| `secretEnv` | `IMAGERIGHT_SECRET_ENV` (`password=CORP_PW,...`): read a secret from another env var | none |
| `requireConfirm` | `IMAGERIGHT_REQUIRE_CONFIRM`: destructive calls need `confirm: <previewId>` | `true` |
| `timeoutSeconds` | `IMAGERIGHT_TIMEOUT_SECONDS` | `30` |
| `caBundle` | `IMAGERIGHT_CA_BUNDLE`: PEM bundle for internal CAs | none |
| `verifyTls` | `IMAGERIGHT_VERIFY_TLS` | `true` |
| `requestIdHeader` | `IMAGERIGHT_REQUEST_ID_HEADER` | `X-Request-Id` |
| `maxRetries` | `IMAGERIGHT_MAX_RETRIES` (GET/HEAD only; writes are never retried) | `2` |
| `outputDir` | `IMAGERIGHT_OUTPUT_DIR`: where binary responses are written | system temp dir |
| `fileRoots` | `IMAGERIGHT_FILE_ROOTS` (`os.pathsep`-separated): folders uploads may be read from | working directory |
| `strictVersion` | `IMAGERIGHT_STRICT_VERSION`: a server version that maps to another profile is an error, not a warning | `false` |
| `requireVerifiedMappings` | `IMAGERIGHT_REQUIRE_VERIFIED_MAPPINGS`: route capabilities only to fixture-verified mappings | `false` |

Set `IMAGERIGHT_CONFIG_FILE` to the path of a JSON file whose keys are the setting names above. The file may
**not** hold secrets; it can only name the environment variables that do (`secretEnv`, `extraHeaders`).
Credentials are read from the environment only, are never accepted as tool arguments, are held in memory
only, and are always redacted in output. User info in `restBaseUrl` (`https://user:pw@host`) is ignored.

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
