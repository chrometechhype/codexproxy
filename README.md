<div align="center">

# CodexProxy

Use the OpenAI Codex CLI and any OpenAI Responses client through your own provider-agnostic proxy. CodexProxy speaks the **OpenAI Responses API** (`POST /v1/responses`) and routes traffic to 17 provider backends.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=for-the-badge)](https://opensource.org/licenses/MIT)
[![Python 3.14](https://img.shields.io/badge/python-3.14-3776ab.svg?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/downloads/)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json&style=for-the-badge)](https://github.com/astral-sh/uv)

</div>

## What You Get

- Drop-in proxy for the OpenAI Responses API consumed by the Codex CLI/Desktop App.
- 17 provider backends: NVIDIA NIM, OpenRouter, Google AI Studio (Gemini), DeepSeek, Mistral La Plateforme, Mistral Codestral, OpenCode Zen, OpenCode Go, Wafer, Kimi, Cerebras Inference, Groq, Fireworks AI, Z.ai, LM Studio, llama.cpp, and Ollama.
- Local **Admin UI** at `/admin` with Codex launcher buttons.
- Streaming, tool use, thinking/reasoning block handling.
- Optional Discord or Telegram bot wrapper for remote sessions.

## Quick Start

### 1. Install

```powershell
# Windows
irm "https://raw.githubusercontent.com/chrometechhype/codexproxy/main/scripts/install.ps1?raw=1" | iex
```

```bash
# macOS/Linux
curl -fsSL "https://raw.githubusercontent.com/chrometechhype/codexproxy/main/scripts/install.sh?raw=1" | sh
```

### 2. Start The Proxy

```bash
cdx-server
```

### 3. Open Admin UI

Navigate to `http://127.0.0.1:9090/admin`. Set your provider API key and model, then click **Launch CLI** or **Launch App** on the **Codex** tab.

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
| `cdx-codex-config` | Write config only (for Desktop App) |
| `cdx-init` | Optional scaffold for advanced setup |

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
