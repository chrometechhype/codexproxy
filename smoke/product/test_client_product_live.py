import json
import os
import shutil
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from smoke.lib.config import SmokeConfig
from smoke.lib.e2e import (
    ClientProtocolDriver,
    ConversationDriver,
    ProviderMatrixDriver,
    SmokeServerDriver,
    assert_product_stream,
)

pytestmark = [pytest.mark.live]


@pytest.mark.smoke_target("clients")
def test_vscode_protocol_e2e(smoke_config: SmokeConfig) -> None:
    provider_model = ProviderMatrixDriver(smoke_config).first_model()
    with SmokeServerDriver(
        smoke_config,
        name="product-vscode",
        env_overrides={
            "MODEL": provider_model.full_model,
            "MESSAGING_PLATFORM": "none",
        },
    ).run() as server:
        turn = ConversationDriver(server, smoke_config).stream(
            ClientProtocolDriver.adaptive_thinking_payload(),
            headers=ClientProtocolDriver.vscode_headers(),
        )

    assert_product_stream(turn.events)


@pytest.mark.smoke_target("clients")
def test_jetbrains_protocol_e2e(smoke_config: SmokeConfig) -> None:
    provider_model = ProviderMatrixDriver(smoke_config).first_model()
    with SmokeServerDriver(
        smoke_config,
        name="product-jetbrains",
        env_overrides={
            "MODEL": provider_model.full_model,
            "MESSAGING_PLATFORM": "none",
        },
    ).run() as server:
        driver = ConversationDriver(server, smoke_config)
        first = driver.stream(
            ClientProtocolDriver.tool_result_payload(),
            headers=ClientProtocolDriver.jetbrains_headers(),
        )

    assert_product_stream(first.events)


@pytest.mark.smoke_target("clients")
def test_opencode_cli_prompt_e2e(smoke_config: SmokeConfig, tmp_path: Path) -> None:
    if not shutil.which("opencode"):
        pytest.skip("missing_env: OpenCode CLI not found")
    uv_bin = shutil.which("uv")
    if not uv_bin:
        pytest.skip("missing_env: uv not found")
    provider_model = ProviderMatrixDriver(smoke_config).first_model()
    auth_token = smoke_config.settings.proxy_auth_token
    isolated_home = tmp_path / "opencode-home"
    isolated_config = tmp_path / "opencode-config"
    for path in (isolated_home, isolated_config):
        path.mkdir()

    with SmokeServerDriver(
        smoke_config,
        name="product-opencode-cli",
        env_overrides={
            "MODEL": provider_model.full_model,
            "ANTHROPIC_AUTH_TOKEN": auth_token,
            "MESSAGING_PLATFORM": "none",
        },
    ).run() as server:
        env = os.environ.copy()
        env.update(
            {
                "HOST": "127.0.0.1",
                "PORT": str(server.port),
                "CODEX_PROXY_OPEN_BROWSER": "0",
                "ANTHROPIC_AUTH_TOKEN": auth_token,
                "HOME": str(isolated_home),
                "USERPROFILE": str(isolated_home),
                "XDG_CONFIG_HOME": str(isolated_home / "config"),
                "XDG_DATA_HOME": str(isolated_home / "data"),
                "XDG_CACHE_HOME": str(isolated_home / "cache"),
                "XDG_STATE_HOME": str(isolated_home / "state"),
                "OPENCODE_CONFIG_DIR": str(isolated_config),
            }
        )
        env.pop("OPENCODE_CONFIG", None)
        env.pop("OPENCODE_CONFIG_CONTENT", None)
        result = subprocess.run(
            [
                uv_bin,
                "run",
                "--project",
                str(smoke_config.root),
                "--no-sync",
                "cdx-opencode",
                "run",
                "--format",
                "json",
                "--model",
                f"codexproxy/{provider_model.full_model}",
                "Reply with exactly CODEX_PROXY_SMOKE_OPENCODE",
            ],
            cwd=tmp_path,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=smoke_config.timeout_s + 15,
        )
        server_log = server.log_path.read_text(encoding="utf-8", errors="replace")

    assert result.returncode == 0, result.stderr or result.stdout
    assert "CODEX_PROXY_SMOKE_OPENCODE" in result.stdout
    assert "POST /v1/responses" in server_log
    assert "POST /v1/chat/completions" not in server_log

