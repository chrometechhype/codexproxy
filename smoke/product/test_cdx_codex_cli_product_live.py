"""Live product smoke for the Codex CLI integration.

The ``cdx-codex`` launcher writes ``~/.codex/config.toml`` with
``wire_api = "responses"`` and ``openai_base_url`` pointing at the running
proxy, then execs the local ``codex`` binary. This smoke boots a local proxy
on a free port, points ``cdx-codex`` at it, and verifies the config.toml the
launcher produced.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from smoke.lib.config import SmokeConfig
from smoke.lib.e2e import SmokeServerDriver

pytestmark = [pytest.mark.live, pytest.mark.smoke_target("cdx_codex_cli")]


def _have_codex_binary() -> bool:
    return shutil.which("codex") is not None


def test_cdx_codex_writes_responses_config_pointing_at_proxy(
    smoke_config: SmokeConfig, tmp_path: Path
) -> None:
    """cdx-codex writes a Responses config that points at the live proxy."""
    if not _have_codex_binary():
        pytest.skip("missing_env: codex binary not on PATH")

    codex_home = tmp_path / "codex_home"
    codex_home.mkdir(parents=True, exist_ok=True)

    user_env = os.environ.copy()
    user_env["CODEX_HOME"] = str(codex_home)
    user_env["CODEX_PROXY_AUTH_TOKEN"] = "freecc"

    with SmokeServerDriver(
        smoke_config,
        name="product-cdx-codex",
        env_overrides={
            "MESSAGING_PLATFORM": "none",
            "CODEX_PROXY_AUTH_TOKEN": "freecc",
        },
    ).run() as server:
        env = dict(user_env)
        env["PYTHONPATH"] = str(smoke_config.root)
        env["HOST"] = "127.0.0.1"
        env["PORT"] = str(server.port)
        env["CODEX_PROXY_HOST"] = "127.0.0.1"
        env["CODEX_PROXY_PORT"] = str(server.port)
        env["PATH"] = (
            str(smoke_config.root / ".venv" / "Scripts")
            + os.pathsep
            + env.get("PATH", "")
        )

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from cli.entrypoints import launch_codex; launch_codex(['--version'])",
            ],
            cwd=smoke_config.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=smoke_config.timeout_s,
            check=False,
        )
        assert result.returncode == 0, (
            f"cdx-codex exited {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

        config_path = codex_home / "config.toml"
        assert config_path.is_file(), (
            f"codex CLI config not written to {config_path}\nstderr: {result.stderr}"
        )

        parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
        provider = parsed["model_providers"]["codexproxy"]
        assert provider["wire_api"] == "responses"
        assert provider["base_url"].rstrip("/") == f"{server.base_url}/v1"
        assert provider["api_key"] == "freecc"
        assert parsed["codexproxy"]["model_provider"] == "codexproxy"


def test_cdx_codex_errors_cleanly_when_proxy_is_down(
    smoke_config: SmokeConfig, tmp_path: Path
) -> None:
    """When the proxy is unreachable, cdx-codex fails with a clear stderr."""
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    env["CODEX_PROXY_AUTH_TOKEN"] = "freecc"
    env["PYTHONPATH"] = str(smoke_config.root)
    env["PATH"] = (
        str(smoke_config.root / ".venv" / "Scripts") + os.pathsep + env.get("PATH", "")
    )

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from cli.entrypoints import launch_codex; launch_codex(['--version'])",
        ],
        cwd=smoke_config.root,
        env=env,
        capture_output=True,
        text=True,
        timeout=smoke_config.timeout_s,
        check=False,
    )

    assert result.returncode == 1
    combined = (result.stdout or "") + (result.stderr or "")
    assert "CodexProxy is not reachable" in combined
    assert not (codex_home / "config.toml").exists()
