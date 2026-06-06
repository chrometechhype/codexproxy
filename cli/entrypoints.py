"""CLI entry points for the installed package."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import uvicorn

from api.admin_urls import local_admin_url, local_proxy_root_url
from api.app import GracefulLifespanApp, create_app
from cli.process_registry import (
    kill_all_best_effort,
    kill_pid_tree_best_effort,
    register_pid,
    unregister_pid,
)
from config.paths import config_dir_path, legacy_env_paths, managed_env_path
from config.settings import Settings, get_settings

PROXY_PREFLIGHT_PATH = "/health"
PROXY_PREFLIGHT_TIMEOUT_SECONDS = 1.5
SERVER_GRACEFUL_SHUTDOWN_SECONDS = 5


def _load_env_template() -> str:
    """Load the canonical root env template from package resources or source."""
    import importlib.resources

    packaged = importlib.resources.files("cli").joinpath("env.example")
    if packaged.is_file():
        return packaged.read_text("utf-8")

    source_template = Path(__file__).resolve().parents[1] / ".env.example"
    if source_template.is_file():
        return source_template.read_text(encoding="utf-8")

    raise FileNotFoundError("Could not find bundled or source .env.example template.")


def serve() -> None:
    """Start the FastAPI server (registered as `cdx-server` script)."""
    opened_admin_browser = False
    try:
        try:
            while True:
                _migrate_legacy_env_if_missing()
                settings = get_settings()
                if not _run_supervised_server(
                    settings, open_admin_browser=not opened_admin_browser
                ):
                    return
                opened_admin_browser = True
                get_settings.cache_clear()
        except KeyboardInterrupt:
            return
    finally:
        kill_all_best_effort()


def _admin_browser_open_enabled() -> bool:
    """Whether to open /admin when the server becomes reachable.

    Reads ``CODEX_PROXY_OPEN_BROWSER`` first and falls back to the legacy
    ``FCC_OPEN_BROWSER`` for one release.
    """

    raw = (
        (
            os.environ.get("CODEX_PROXY_OPEN_BROWSER")
            or os.environ.get("FCC_OPEN_BROWSER", "true")
        )
        .strip()
        .lower()
    )
    return raw not in {"", "0", "false", "no"}


def _schedule_open_admin_browser(settings: Settings) -> None:
    """After /health succeeds, open the admin UI in the default browser (daemon thread)."""

    if not _admin_browser_open_enabled():
        return

    admin_url = local_admin_url(settings)
    proxy_root_url = local_proxy_root_url(settings)

    def open_when_ready() -> None:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if _preflight_proxy(proxy_root_url) is None:
                webbrowser.open(admin_url)
                return
            time.sleep(0.15)

    threading.Thread(
        target=open_when_ready, name="cdx-open-admin-browser", daemon=True
    ).start()


def _run_supervised_server(settings: Settings, *, open_admin_browser: bool) -> bool:
    """Run one uvicorn server instance; return whether admin requested restart."""

    restart_requested = False
    server_holder: dict[str, uvicorn.Server] = {}

    def request_restart() -> None:
        nonlocal restart_requested
        restart_requested = True
        if server := server_holder.get("server"):
            server.should_exit = True

    app = create_app(lifespan_enabled=False)
    app.state.admin_restart_callback = request_restart
    asgi_app = GracefulLifespanApp(app)
    config = uvicorn.Config(
        asgi_app,
        host=settings.host,
        port=settings.port,
        log_level="debug",
        timeout_graceful_shutdown=SERVER_GRACEFUL_SHUTDOWN_SECONDS,
    )
    server = uvicorn.Server(config)
    server_holder["server"] = server
    if open_admin_browser:
        _schedule_open_admin_browser(settings)
    server.run()
    return restart_requested


def init() -> None:
    """Scaffold config at ~/.codexproxy/.env (registered as `cdx-init`)."""
    config_dir = config_dir_path()
    env_file = managed_env_path()

    migrated_from = _migrate_legacy_env_if_missing()
    if migrated_from is not None:
        print(f"Config migrated from {migrated_from} to {env_file}")
        print(
            "Edit it to set your API keys and model preferences, then run: cdx-server"
        )
        return

    if env_file.exists():
        print(f"Config already exists at {env_file}")
        print("Delete it first if you want to reset to defaults.")
        return

    config_dir.mkdir(parents=True, exist_ok=True)
    template = _load_env_template()
    env_file.write_text(template, encoding="utf-8")
    print(f"Config created at {env_file}")
    print("Edit it to set your API keys and model preferences, then run: cdx-server")


def _migrate_legacy_env_if_missing() -> Path | None:
    """Copy a legacy user env into the managed config path when absent."""

    env_file = managed_env_path()
    if env_file.exists():
        return None

    # TODO: Remove after the ~/.codexproxy/.env migration has had a release cycle.
    for legacy_env in legacy_env_paths():
        if not legacy_env.is_file():
            continue
        env_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(legacy_env, env_file)
        return legacy_env

    return None


def _claude_child_env(
    settings: Settings, base_env: Mapping[str, str]
) -> dict[str, str]:
    """Return a Claude Code environment that targets this proxy.

    Retained for one release as a compatibility shim while ``cdx-codex`` is being
    implemented. The legacy Claude Code client still speaks the Anthropic
    Messages API, so the proxy needs the same env vars it used before the
    rename.
    """

    env = {
        key: value
        for key, value in base_env.items()
        if not key.startswith("ANTHROPIC_")
    }
    env.pop("ANTHROPIC_API_KEY", None)
    env["ANTHROPIC_BASE_URL"] = local_proxy_root_url(settings)
    env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] = "1"
    env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = "190000"
    if token := settings.effective_auth_token:
        env["ANTHROPIC_AUTH_TOKEN"] = token
    return env


def _codex_child_env(settings: Settings, base_env: Mapping[str, str]) -> dict[str, str]:
    """Return a Codex CLI environment that targets this proxy.

    Codex honours ``OPENAI_BASE_URL`` (deprecated) and the
    ``openai_base_url`` config key in ``~/.codex/config.toml``. We set
    ``OPENAI_BASE_URL`` for the simple shim path; the full integration in
    Phase 6 writes a proper ``config.toml`` with ``wire_api = "responses"``.
    """

    env = dict(base_env)
    env["OPENAI_BASE_URL"] = local_proxy_root_url(settings)
    if token := settings.effective_auth_token:
        env["OPENAI_API_KEY"] = token
    return env


def _preflight_proxy(proxy_root_url: str) -> str | None:
    """Return an error message when the local proxy health check is unreachable."""

    url = f"{proxy_root_url.rstrip('/')} {PROXY_PREFLIGHT_PATH}"
    request = Request(url, method="GET")
    try:
        with urlopen(request, timeout=PROXY_PREFLIGHT_TIMEOUT_SECONDS) as response:
            status_code = response.getcode()
    except HTTPError as exc:
        return f"returned HTTP {exc.code}"
    except URLError as exc:
        return str(exc.reason)
    except OSError as exc:
        return str(exc)

    if not 200 <= status_code < 300:
        return f"returned HTTP {status_code}"
    return None


def _codex_config_path() -> Path:
    """Return the path of the Codex CLI ``config.toml`` for the current user."""
    return Path.home() / ".codex" / "config.toml"


def _codex_config_path_alt() -> Path:
    """Return the alt Codex config path (Windows ``%CODEX_HOME%/config.toml``)."""
    custom = os.environ.get("CODEX_HOME")
    if custom:
        return Path(custom) / "config.toml"
    return _codex_config_path()


def _codex_config_backup_path(config_path: Path) -> Path:
    """Return the path of the pre-CodexProxy backup for ``config.toml``."""
    return config_path.with_name(f"{config_path.name}.codexproxy-backup")


def _codex_auth_json_path() -> Path:
    """Return the path of the Codex CLI ``auth.json`` for the current user."""
    custom = os.environ.get("CODEX_HOME")
    return (Path(custom) if custom else Path.home() / ".codex") / "auth.json"


def _codex_auth_json_backup_path(auth_path: Path) -> Path:
    """Return the path of the pre-CodexProxy backup for ``auth.json``."""
    return auth_path.with_name(f"{auth_path.name}.codexproxy-backup")


def _toml_quote(value: str) -> str:
    """Render a string for embedding inside a double-quoted TOML literal."""
    return value.replace("\\", "\\\\").replace('\"', '\\\"')


def _update_top_level_codex_settings(
    text: str, *, model: str, provider: str = "codexproxy"
) -> str:
    """Update top-level ``model`` and ``model_provider`` in a Codex config file.

