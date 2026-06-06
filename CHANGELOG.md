# Changelog

All notable changes to `codexproxy` are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/).

## [1.1.0] - 2026-06-05

Codex Desktop App (`codex app`) now routes through CodexProxy alongside
`codex exec`.

### Added
- `cdx-codex-config` entry point: writes `~/.codex/config.toml` and exits,
  for users who run the Codex Desktop App without spawning `codex exec`.
- Top-level `model` and `model_provider` rewriter in `_write_codex_config`:
  when the user's existing `config.toml` has a stale `model = "..."`, the
  launcher now replaces it with the configured `MODEL` (with the
  `provider/` prefix stripped) so the desktop app picks a real model on
  its next refresh.
- 5 new unit tests in `tests/cli/test_cdx_codex.py` covering the
  rewriter, the `cdx-codex-config` entry point, and the
  "preserves-user-sections" property of the new behavior.
- 2 new live smoke tests in `smoke/product/test_cdx_codex_cli_product_live.py`
  verifying that both `cdx-codex` and `cdx-codex-config` rewrite the stale
  top-level `model` while preserving unrelated sections.

### Fixed
- `_responses_input_to_messages` in `api/responses_service.py` now translates
  Responses-style content blocks (`input_text` / `input_image`) to the
  Anthropic wire format (`text` / `image`). The Codex CLI v0.136+ ships
  `{"type": "input_text", "text": "..."}` items in `input[]`, which the
  Anthropic `Message.content` Pydantic model rejected with a `literal_error`,
  so `codex exec` was failing with `stream disconnected before completion`.
  The new helper (`_responses_content_to_anthropic`) uses Pydantic's
  `TypeAdapter` against the Anthropic content-block union, so unknown blocks
  are skipped instead of crashing.
- 7 new unit tests in `tests/api/test_responses_routes.py` covering
  `input_text` / `input_image` translation, multi-block lists, plain strings,
  `None` content, and unknown-block skip behaviour.

### Changed
- README now documents the Codex Desktop App integration under
  "Connect The Codex CLI".
- `docs/responses-migration.md` documents the top-level `model` / `model_provider`
  rewrite and the Codex Desktop App quirks (the internal app-server re-reads
  `config.toml` on every conversation).

## [1.0.0] - 2026-06-05

First stable release of `codexproxy`. The fork of `free-claude-code` is
feature-complete for the OpenAI Responses API: the Codex CLI runs end-to-end
through the proxy, all 17 providers are reachable, and the legacy
`/v1/messages` Anthropic surface is preserved as a deprecation shim.

### Added
- `POST /v1/responses`, `GET /v1/responses/{id}`, and
  `GET /v1/responses/{id}/input_items` routes backed by an in-memory
  `ResponseStore` (1-hour TTL).
- `AnthropicToResponsesAdapter` that consumes provider SSE from the existing
  transport stack and re-emits standard Responses events.
- `cdx-codex` launcher that writes `~/.codex/config.toml` with
  `wire_api = "responses"` and execs the Codex CLI through the proxy.
- New settings: `CODEX_PROXY_EXTRA_MODEL_IDS`, `CODEX_PROXY_PROMPT_CACHING`,
  `CODEX_PROXY_USE_PREVIOUS_RESPONSE_ID`.
- `docs/responses-migration.md` migration log.

### Changed
- Rebrand complete: package is `codexproxy`, scripts are `cdx-server`,
  `cdx-init`, `cdx-codex`, user config dir is `~/.codexproxy/`, auth env var
  is `CODEX_PROXY_AUTH_TOKEN`.
- All 17 provider backends reused unchanged (Anthropic Messages transports
  reach the Responses adapter without provider-specific plumbing).
- README rewritten around `codex exec` and the OpenAI Responses API.

## [0.7.0] - 2026-06-05

### Added
- `cdx-codex` writes `~/.codex/config.toml` with `wire_api = "responses"`,
  `openai_base_url = http://127.0.0.1:PORT/v1`, and the configured auth token
  as `OPENAI_API_KEY`, then execs the `codex` binary. The writer is idempotent
  and preserves any pre-existing user settings.
- New settings: `CODEX_PROXY_EXTRA_MODEL_IDS`, `CODEX_PROXY_PROMPT_CACHING`,
  `CODEX_PROXY_USE_PREVIOUS_RESPONSE_ID` for Responses API configuration.
- `docs/responses-migration.md` documenting the migration to the OpenAI
  Responses API.
- Tests for the config writer, the launcher, and the proxy-unreachable error
  path (`tests/cli/test_cdx_codex.py`, 14 tests).
- Live smoke tests for the Codex CLI integration
  (`smoke/product/test_cdx_codex_cli_product_live.py`, 2 tests).

### Changed
- `scripts/install.sh` and `scripts/install.ps1` now install the Codex CLI
  (`npm i -g @openai/codex`) and the `codexproxy` package via
  `uv tool install`. Final message references `cdx-server` and `cdx-codex`.
- `README.md` rewritten around the Codex CLI and the OpenAI Responses API;
  per-tier `MODEL_OPUS` / `MODEL_SONNET` / `MODEL_HAIKU` references removed.
- `tests/scripts/test_installers.py` rewritten to assert Codex CLI install and
  `codexproxy` package spec (12 tests).

## [0.6.0] - 2026-06-05

### Added
- `tests/test_cdx_codex_smoke_target.py` registering the `cdx_codex_cli` smoke
  target alongside the existing CLI product smokes.

## [0.5.0] - 2026-06-05

### Fixed
- `AnthropicToResponsesAdapter.finalize()` no longer returns an empty list on
  the second invocation. Idempotency is now driven by a `_completed_emitted`
  flag; the first call always flushes unclosed blocks and emits the terminal
  event.

### Added
- 14 unit tests in `tests/cli/test_cdx_codex.py` covering the config writer,
  the launcher, and the proxy-unreachable error path.

## [0.4.0] - 2026-06-05

### Added
- `cdx-codex` launcher that writes `~/.codex/config.toml` and execs the
  Codex CLI through the proxy.

## [0.3.0] - 2026-06-05

### Added
- New `CODEX_PROXY_*` settings: `CODEX_PROXY_EXTRA_MODEL_IDS`,
  `CODEX_PROXY_PROMPT_CACHING`, `CODEX_PROXY_USE_PREVIOUS_RESPONSE_ID`.

## [0.2.0] - 2026-06-05

### Added
- `core/responses/` package: Pydantic Responses data models, SSE event
  builders, the `AnthropicToResponsesAdapter`, and an in-memory `ResponseStore`
  with TTL eviction.
- `/v1/responses` route (streaming and non-streaming) that routes every Codex
  request through the existing provider transport stack and translates the
  upstream Anthropic-format SSE into Responses-format SSE.
- `/v1/responses/{id}` and `/v1/responses/{id}/input_items` retrieval routes
  backed by a shared `ResponseStore` (stored on `app.state`).
- `POST /v1/conversations` and `GET /v1/responses` stub routes.
- Tests for the Responses data models, SSE builders, the Anthropic→Responses
  adapter, the response store, and the route layer.

### Changed
- `AnthropicToResponsesAdapter.finalize()` no longer short-circuits on
  `_completed`; it always flushes unclosed blocks and emits the terminal
  `response.completed` (or `response.incomplete` on error) event.
- `AppRuntime.startup` now creates a single shared `ResponseStore` instance on
  `app.state`, so retrieval routes can find responses created by other
  requests.

## [0.1.2] - 2026-06-05

### Removed
- Per-tier `MODEL_OPUS` / `MODEL_SONNET` / `MODEL_HAIKU` settings and
  `ENABLE_OPUS_THINKING` / `ENABLE_SONNET_THINKING` / `ENABLE_HAIKU_THINKING`
  switches. `resolve_model` now always returns the configured default `MODEL`,
  and `resolve_thinking` returns `enable_model_thinking`.

### Changed
- `configured_chat_model_refs` returns a single chat model ref sourced from
  `MODEL` instead of a list of tier-specific refs.

## [0.1.1] - 2026-06-05

### Changed
- Admin UI copy rebranded to `CodexProxy` with the `CX` brand mark.
- AGENTS.md / CLAUDE.md updated to describe the `codexproxy` fork.
- `start.bat` project-root launcher added with `EnableDelayedExpansion` so the
  window stays open on Ctrl+C and prints the exit code.

## [0.1.0] - 2026-06-05

### Added
- Initial rebrand of `free-claude-code` to `codexproxy` v0.1.0.
- New package metadata: `name = "codexproxy"`, scripts `cdx-server`,
  `cdx-init`, `cdx-codex` (Phase 1 stub).
- New settings: `codex_workspace`, `codex_cli_bin`, `codex_proxy_auth_token`
  (with deprecated aliases `claude_workspace`, `claude_cli_bin`,
  `anthropic_auth_token`).
- New `~/.codexproxy/` user config dir, replacing `~/.claude-code/`.
- New `CODEX_PROXY_*` env-var prefix (with `FCC_*` / `ANTHROPIC_*` kept as
  deprecated aliases for one release).
