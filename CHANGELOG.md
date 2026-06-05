# Changelog

All notable changes to `codexproxy` are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/).

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
