# Responses API migration

This document tracks the migration from the Anthropic Messages API (`/v1/messages`) to the OpenAI Responses API (`/v1/responses`).

## Why

`free-claude-code` originally proxied Claude Code traffic through the Anthropic Messages API. The rebrand to `codexproxy` targets the OpenAI Responses API consumed by the Codex CLI (`codex exec`).

## Wire surface

| Surface | Status | Notes |
| --- | --- | --- |
| `POST /v1/responses` | Implemented (v0.2.0) | Streaming + non-streaming. |
| `GET /v1/responses/{id}` | Implemented (v0.2.0) | Backed by `core.responses.store.ResponseStore` (1-hour TTL). |
| `GET /v1/responses/{id}/input_items` | Implemented (v0.2.0) | Echoes the input items from the original create request. |
| `POST /v1/conversations` | Stub (v0.2.0) | Returns `conv_<timestamp>` so clients that gate on this endpoint succeed. |
| `GET /v1/models` | Implemented (v0.2.0) | Advertises the configured `MODEL` plus any `CODEX_PROXY_EXTRA_MODEL_IDS`. |
| `POST /v1/messages` | Legacy | Anthropic shim kept for one release; will be removed in a later version. |
| `POST /v1/messages/count_tokens` | Legacy | Anthropic shim kept for one release. |

## Provider transport

All 17 providers are wired through a single `AnthropicToResponsesAdapter` that consumes provider SSE (Anthropic Messages or OpenAI Chat) and emits Responses SSE. No per-provider Responses plumbing is required for the basic endpoint to work.

The `responses_native` provider (direct OpenAI Responses passthrough) is **deferred** to a later release because it would require a new `TransportType` literal in `config/provider_catalog.py` and a refactor of `providers/registry.py::PROVIDER_CATALOG`.

## Settings

| Setting | Env | Default | Notes |
| --- | --- | --- | --- |
| `MODEL` | `MODEL` | `nvidia_nim/nvidia/nemotron-3-super-120b-a12b` | The only model advertised on `/v1/models`. |
| `ENABLE_MODEL_THINKING` | `ENABLE_MODEL_THINKING` | `false` | Provider-agnostic thinking toggle. |
| `CODEX_PROXY_EXTRA_MODEL_IDS` | `CODEX_PROXY_EXTRA_MODEL_IDS` | unset | Comma-separated extra model ids advertised on `/v1/models`. |
| `CODEX_PROXY_PROMPT_CACHING` | `CODEX_PROXY_PROMPT_CACHING` | `true` | Best-effort prompt caching on the upstream. |
| `CODEX_PROXY_USE_PREVIOUS_RESPONSE_ID` | `CODEX_PROXY_USE_PREVIOUS_RESPONSE_ID` | `true` | Best-effort `previous_response_id` chain follow-through. |

Deprecated (one-release alias for `CODEX_PROXY_AUTH_TOKEN`):

- `ANTHROPIC_AUTH_TOKEN`
- `FCC_OPEN_BROWSER`
- `FCC_ENV_FILE`
- `FCC_SMOKE_TARGETS`

## Launcher

`cdx-codex` writes `~/.codex/config.toml` with the following managed block:

```toml
[model_providers.codexproxy]
name = "codexproxy"
base_url = "http://127.0.0.1:8082/v1"
api_key = "freecc"
wire_api = "responses"
requires_openai_auth = true

[model_providers.codexproxy.env]
OPENAI_API_KEY = "freecc"

[codexproxy]
model = "<bare model id>"
model_provider = "codexproxy"
approval_policy = "never"
sandbox_mode = "workspace-write"
```

The launcher is **idempotent**: re-running `cdx-codex` replaces only the `codexproxy` block; any other user settings in `~/.codex/config.toml` are preserved.

`CODEX_HOME` overrides the Codex config directory. The launcher also respects `OPENAI_BASE_URL`/`OPENAI_API_KEY` from the parent env so users can pre-pin a different target if needed.

### Top-level `model` and `model_provider` (Codex Desktop App)

`cdx-codex` also rewrites the **top-level** `model` and `model_provider` keys in `~/.codex/config.toml` (the prefix before the first `[section]` header). This is what the Codex Desktop App (`codex app`) reads as the conversation default. Without the rewrite, a stale `model = "proxy-model"` would cause the desktop app to request a model the proxy does not advertise.

For users who run only the Codex Desktop App and never `codex exec`, the dedicated entry point `cdx-codex-config` writes the same config and exits without spawning a child process. The desktop app-server reads `config.toml` on every conversation, so the change takes effect on the next turn without a restart in most cases.

### Codex CLI quirks

- Codex CLI 0.118+ ignores the `OPENAI_BASE_URL` environment variable. The launcher must write the URL into `~/.codex/config.toml` (see [openai/codex#16719](https://github.com/openai/codex/issues/16719)).
- The Codex CLI's internal `codex_apps` MCP server hits `/v1/responses`; it inherits the auth token from `~/.codex/config.toml`.
- The Codex Desktop App's internal `app-server` (a bundled `codex.exe` started over stdio JSON-RPC) re-reads `~/.codex/config.toml` on every conversation, so `cdx-codex-config` updates are visible without restarting the desktop app.

## Testing

- Unit tests in `tests/cli/test_cdx_codex.py` cover the config writer, the top-level `model` / `model_provider` rewriter, the `cdx-codex` launcher, the `cdx-codex-config` entry point, and the proxy-unreachable error path.
- Live smoke tests in `smoke/product/test_cdx_codex_cli_product_live.py` boot the proxy and assert that the `config.toml` written by both `cdx-codex` and `cdx-codex-config` is parseable, points at the right base URL, and replaces the user's stale top-level `model` while preserving unrelated sections.
- 39 tests in `tests/core/responses/` and `tests/api/test_responses_routes.py` cover the SSE encoder, the adapter, the store, and the route surface.

## Future work

- `responses_native` provider for direct OpenAI Responses passthrough.
- Persistent response store (SQLite/Redis) with TTL eviction.
- Strict-mode `previous_response_id` chain validation.
- Function-call tools (`web_search`, `web_fetch`) wire-up.
- Removal of the legacy `/v1/messages` Anthropic shim.
