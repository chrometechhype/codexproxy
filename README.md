<div align="center">

# CodexProxy

Use the OpenAI Codex CLI and any OpenAI Responses client through your own provider-agnostic proxy. CodexProxy speaks the **OpenAI Responses API** (`POST /v1/responses`) and routes traffic to 17 provider backends.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=for-the-badge)](https://opensource.org/licenses/MIT)
[![Python 3.14](https://img.shields.io/badge/python-3.14-3776ab.svg?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/downloads/)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json&style=for-the-badge)](https://github.com/astral-sh/uv)

**Last update: 2026-06-07 — v1.4.0**

</div>

## What's New in v1.4.0

- **Keepalive pings**: Provider idle no longer causes Codex CLI to hang — periodic `response.in_progress` events keep the connection alive during long model thinking
- **Multi-turn stability**: Full tool-call → tool-result → response cycle tested end-to-end
- Improved streaming reliability for Gemini and other slow-reasoning models

## What You Get

- Drop-in proxy for the OpenAI Responses API consumed by the Codex CLI/Desktop App
- 17 provider backends: NVIDIA NIM, OpenRouter, Google AI Studio (Gemini), DeepSeek, Mistral La Plateforme, Mistral Codestral, OpenCode Zen, OpenCode Go, Wafer, Kimi, Cerebras Inference, Groq, Fireworks, Z.ai, LM Studio, Llama.cpp, Ollama
- Local **Admin UI** at `/admin` with Codex launcher buttons
- Streaming, tool use, thinking/reasoning block handling
- Optional Discord or Telegram bot wrapper for remote sessions

## Quick Start

### 1. Install

**Note: This project is currently only supported on Windows.**

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

Set the `CODEX_PROXY_PORT` environment variable before starting the server:

```powershell
$env:CODEX_PROXY_PORT = "9090"
cdx-server
```

### Do I need an API key?

Yes — each provider requires its own API key. Set it in the Admin UI or via environment variables (see `.env.example`). By default the proxy itself requires `ANTHROPIC_AUTH_TOKEN` for authentication; you can disable this by setting the same token in `.env`.

### Can I run multiple models at the same time?

No, CodexProxy currently supports a single active model at a time. Use the Admin UI to switch.

### Why Windows-only?

Codex CLI itself is Windows-native (UWP). The installer, config paths, and launchers are Windows-specific. Linux/macOS support is possible but not planned yet.

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
