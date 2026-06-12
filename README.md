<div align="center">

# CodexProxy

Use the OpenAI Codex CLI and any OpenAI Responses client through your own provider-agnostic proxy. CodexProxy speaks the **OpenAI Responses API** (`POST /v1/responses`) and routes traffic to 17 provider backends.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=for-the-badge)](https://opensource.org/licenses/MIT)
[![Python 3.14](https://img.shields.io/badge/python-3.14-3776ab.svg?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/downloads/)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json&style=for-the-badge)](https://github.com/astral-sh/uv)

**Last update: 2026-06-12 — v1.10.0**

</div>

## What's New in v1.10.0

- **Linux support** — CodexProxy now runs on Linux. The installer (`scripts/install.sh`), all CLI commands, and the proxy server itself are fully cross-platform. The Codex CLI (`cdx-codex`) and proxy server (`cdx-server`) work on Linux out of the box.

## What's New in v1.9.0

- **`/v1/chat/completions` endpoint** — OpenAI Chat Completions ↔ Responses API adapter. Codex CLI and any OpenAI SDK client can now use the proxy via the standard chat completions surface.
- **SQLite response store** — optional persistent store (`responses_store_backend=sqlite`). Enables `GET /v1/responses/{id}` across restarts.
- **Native `apply_patch` tool conversion** — Codex CLI's `{"type": "apply_patch"}` is automatically converted to a function tool for non-OpenAI providers.
- **Improved SSE parsing** — line-based state machine replaces regex-based parser. Handles `\n\n` inside event data correctly.
- **Stream retry/failover** — `_producer()` retries once on transient errors (timeout, connection reset).
- **`completed_at` fix** — timestamp now reflects actual completion time, not request creation time.
- **`asyncio.Queue` maxsize** bumped from 1 to 100 for better throughput under load.

## What You Get

- Drop-in proxy for the OpenAI Responses API consumed by the Codex CLI/Desktop App
- 17 provider backends: NVIDIA NIM, OpenRouter, Google AI Studio (Gemini), DeepSeek, Mistral La Plateforme, Mistral Codestral, OpenCode Zen, OpenCode Go, Wafer, Kimi, Cerebras Inference, Groq, Fireworks, Z.ai, LM Studio, Llama.cpp, Ollama
- Local **Admin UI** at `/admin` with Codex launcher buttons
- Streaming, tool use, thinking/reasoning block handling
- Optional Discord or Telegram bot wrapper for remote sessions

## Quick Start

### 1. Install

#### Linux
```bash
curl -LsSf "https://raw.githubusercontent.com/chrometechhype/codexproxy/main/scripts/install.sh" | sh
```

#### Windows
```powershell
irm "https://raw.githubusercontent.com/chrometechhype/codexproxy/main/scripts/install.ps1?raw=1" | iex
```

### 2. Start The Proxy

```bash
cdx-server
```

### 3. Open Admin UI

Navigate to `http://127.0.0.1:8083/admin`. Set your provider API key and model, then click **Launch CLI** or **Launch App** on the **Codex** tab.

## Choose A Provider

Set `MODEL` to a provider-prefixed slug. Examples:

| Provider | Model slug |
|----------|-----------|
| NVIDIA NIM | `nvidia_nim/nvidia/nemotron-3-super-120b-a12b` |
| OpenRouter | `open_router/openrouter/free` |
| Gemini | `gemini/models/gemini-3.1-flash-lite` |
| DeepSeek | `deepseek/deepseek-chat` |
| OpenCode Zen | `opencode/big-pickle` (free), `opencode/deepseek-v4-flash-free` (free) |
| OpenCode Go | `opencode_go/minimax-m2.7` |
| Wafer | `wafer/DeepSeek-V4-Pro` |

## Commands

| Command | Purpose |
|---------|---------|
| `cdx-server` | Start the proxy |
| `cdx-codex` | Write config + launch CLI |
| `cdx-codex-app` | Write config + launch Desktop App |
| `cdx-codex-config` | Write config only (for Desktop App) |
| `cdx-restore` | Restore pre-CodexProxy configuration |
| `cdx-delete` | Complete removal of all CodexProxy files |
| `cdx-init` | Optional scaffold for advanced setup |

## FAQ

### Why does Codex CLI seem to hang after running one command?

This was a known issue in v1.3.x when using models that take a long time to think (especially Gemini). When the model sits idle for 30-60 seconds, Codex CLI sees a silent SSE connection and appears frozen. **v1.4.0 fixes this** with automatic keepalive pings every 15 seconds. Update to the latest version.

### Which providers are free?

- **OpenCode Zen** (`opencode/big-pickle`, `opencode/deepseek-v4-flash-free`) — free tier available
- **OpenRouter** (`open_router/openrouter/free`) — some free models
- **Gemini** (`gemini/models/gemini-3.1-flash-lite`) — free tier with rate limits

### Can I use CodexProxy without the Codex CLI?

Yes. CodexProxy is a standard OpenAI Responses API server. Any HTTP client that speaks `POST /v1/responses` can use it.

### How do I change the port?

Set `CODEX_PROXY_PORT` before starting:

```bash
# Linux
export CODEX_PROXY_PORT=9090
cdx-server

# Windows PowerShell
$env:CODEX_PROXY_PORT = "9090"
cdx-server
```

Or pass it inline:

```bash
# Linux
CODEX_PROXY_PORT=9090 cdx-server

# Windows
$env:CODEX_PROXY_PORT = "9090"; cdx-server
```

### Do I need an API key?

Yes — each provider requires its own API key. Set it in the Admin UI or via environment variables (see `.env.example`). By default the proxy itself requires `CODEX_PROXY_AUTH_TOKEN` for authentication; you can disable this by setting it in `.env`.

### Can I run multiple models at the same time?

No, CodexProxy currently supports a single active model at a time. Use the Admin UI to switch.

### Which platforms are supported?

**Linux** and **Windows** are fully supported.

**Important:** On Linux only the npm version of Codex CLI is available (`npm install -g @openai/codex`). The Codex Desktop App is Windows-only (UWP). All CLI commands (`cdx-codex`, `cdx-server`, `cdx-init`, etc.) work identically on both platforms.

## Development

```bash
git clone https://github.com/chrometechhype/codexproxy.git
cd codexproxy
uv run uvicorn server:app --reload
```

Run checks before pushing:

```bash
uv run ruff format
uv run ruff check
uv run ty check
uv run pytest
```

## License

MIT License. See [LICENSE](LICENSE).
