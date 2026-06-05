"""CLI entry points for the installed package."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Mapping, Sequence
from pathlib import Path
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
    """Start the FastAPI server (registered as `fcc-server` script)."""
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
        target=open_when_ready, name="fcc-open-admin-browser", daemon=True
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

    # TODO: Remove after the ~/.fcc/.env migration has had a release cycle.
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

    url = f"{proxy_root_url.rstrip('/')}{PROXY_PREFLIGHT_PATH}"
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


def _toml_quote(value: str) -> str:
    """Render a string for embedding inside a double-quoted TOML literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _write_codex_config(
    config_path: Path, *, base_url: str, api_key: str, model: str
) -> None:
    """Write or update ``config.toml`` so Codex CLI targets this proxy.

    The Codex CLI reads ``openai_base_url`` and ``api_key`` from the
    ``[model_providers.codexproxy]`` table; the top-level ``model_provider`` and
    ``model`` keys select that provider. We never delete pre-existing user
    settings — we only update the ``codexproxy`` block.
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing = ""
    if config_path.is_file():
        try:
            existing = config_path.read_text(encoding="utf-8")
        except OSError:
            existing = ""

    block = (
        "\n# >>> codexproxy (managed by cdx-codex) >>>\n"
        "[model_providers.codexproxy]\n"
        f'name = "codexproxy"\n'
        f'base_url = "{_toml_quote(base_url)}"\n'
        f'api_key = "{_toml_quote(api_key)}"\n'
        "wire_api = \"responses\"\n"
        "requires_openai_auth = true\n"
        "\n"
        "[model_providers.codexproxy.env]\n"
        f'OPENAI_API_KEY = "{_toml_quote(api_key)}"\n'
        "\n"
        "[codexproxy]\n"
        f'model = "{_toml_quote(model)}"\n'
        f'model_provider = "codexproxy"\n'
        f'approval_policy = "never"\n'
        f'sandbox_mode = "workspace-write"\n'
        "# <<< codexproxy <<<\n"
    )
    if "# >>> codexproxy (managed by cdx-codex) >>>" in existing:
        head, _, _ = existing.partition("# >>> codexproxy (managed by cdx-codex) >>>")
        merged = head.rstrip() + "\n" + block
    else:
        merged = existing.rstrip() + "\n" + block if existing.strip() else block

    config_path.write_text(merged, encoding="utf-8")


def _default_codex_model(settings: Settings) -> str:
    """Return the model id Codex CLI should default to.

    Codex CLI requires the model to look like an OpenAI Responses id (e.g.
    ``gpt-4o``). We expose the bare model id (without the ``provider/``
    prefix) so the proxy's ``/v1/models`` and the configured ``MODEL`` line up.
    """
    raw = settings.model.strip()
    if "/" in raw:
        return raw.split("/", 1)[1] or "gpt-4o"
    return raw or "gpt-4o"


def launch_codex(argv: Sequence[str] | None = None) -> None:
    """Launch the OpenAI Codex CLI through this proxy.

    Writes ``~/.codex/config.toml`` with ``wire_api = "responses"``,
    ``openai_base_url`` pointing at the running proxy, and the configured
    auth token as ``OPENAI_API_KEY``, then exec's the ``codex`` binary.
    """

    settings = get_settings()
    proxy_root_url = local_proxy_root_url(settings)
    if error := _preflight_proxy(proxy_root_url):
        print(
            f"CodexProxy is not reachable at {proxy_root_url}: {error}",
            file=sys.stderr,
        )
        print("Start it in another terminal with: cdx-server", file=sys.stderr)
        raise SystemExit(1)

    token = settings.effective_auth_token or "freecc"
    config_path = _codex_config_path_alt()
    _write_codex_config(
        config_path,
        base_url=f"{proxy_root_url.rstrip('/')}/v1",
        api_key=token,
        model=_default_codex_model(settings),
    )
    print(f"Codex CLI config written to {config_path}", file=sys.stderr)

    args = list(sys.argv[1:] if argv is None else argv)
    codex_command = shutil.which(settings.codex_cli_bin)
    if codex_command is None:
        print(
            f"Could not find Codex CLI command: {settings.codex_cli_bin}",
            file=sys.stderr,
        )
        print(
            "Install Codex CLI from https://github.com/openai/codex",
            file=sys.stderr,
        )
        raise SystemExit(127)

    command = [codex_command, *args]
    env = _codex_child_env(settings, os.environ)
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(command, env=env)
        if process.pid:
            register_pid(process.pid)
        return_code = process.wait()
    except FileNotFoundError:
        print(
            f"Could not find Codex CLI command: {settings.codex_cli_bin}",
            file=sys.stderr,
        )
        print(
            "Install Codex CLI from https://github.com/openai/codex",
            file=sys.stderr,
        )
        raise SystemExit(127) from None
    except KeyboardInterrupt:
        if process is not None and process.pid:
            kill_pid_tree_best_effort(process.pid)
            process.wait()
        raise
    finally:
        if process is not None and process.pid:
            unregister_pid(process.pid)

    raise SystemExit(return_code)


def launch_claude(argv: Sequence[str] | None = None) -> None:
    """Launch the legacy Claude Code CLI through this proxy.

    Retained for one release so existing users can keep running Claude Code
    while the Codex migration is in progress. The proxy still speaks the
    Anthropic Messages API; this is the old behaviour under the new name.
    """

    settings = get_settings()
    proxy_root_url = local_proxy_root_url(settings)
    if error := _preflight_proxy(proxy_root_url):
        print(
            f"CodexProxy is not reachable at {proxy_root_url}: {error}",
            file=sys.stderr,
        )
        print("Start it in another terminal with: cdx-server", file=sys.stderr)
        raise SystemExit(1)

    args = list(sys.argv[1:] if argv is None else argv)
    claude_command = shutil.which(settings.codex_cli_bin)  # legacy binary lookup
    claude_command = (
        shutil.which("claude") if claude_command is None else claude_command
    )
    if claude_command is None:
        print(
            "Could not find Claude Code command: claude",
            file=sys.stderr,
        )
        print(
            "Install Claude Code with: npm install -g @anthropic-ai/claude-code",
            file=sys.stderr,
        )
        raise SystemExit(127)

    command = [claude_command, *args]
    env = _claude_child_env(settings, os.environ)
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(command, env=env)
        if process.pid:
            register_pid(process.pid)
        return_code = process.wait()
    except FileNotFoundError:
        print(
            "Could not find Claude Code command: claude",
            file=sys.stderr,
        )
        print(
            "Install Claude Code with: npm install -g @anthropic-ai/claude-code",
            file=sys.stderr,
        )
        raise SystemExit(127) from None
    except KeyboardInterrupt:
        if process is not None and process.pid:
            kill_pid_tree_best_effort(process.pid)
            process.wait()
        raise
    finally:
        if process is not None and process.pid:
            unregister_pid(process.pid)

    raise SystemExit(return_code)
