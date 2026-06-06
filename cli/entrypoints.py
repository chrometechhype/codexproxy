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
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _update_top_level_codex_settings(
    text: str, *, model: str, provider: str = "codexproxy"
) -> str:
    """Update top-level ``model`` and ``model_provider`` in a Codex config file.

    The Codex CLI (and the Codex Desktop app) treat ``model`` and
    ``model_provider`` at the top of the file as the conversation defaults.
    We split the file at the first ``[section]`` header and only touch the
    top-level prefix, leaving every user-defined section, comment, and key
    unchanged. If either key is missing we insert it at the top of the file.
    """

    section_match = re.search(r"^\s*\[", text, re.MULTILINE)
    prefix = text if section_match is None else text[: section_match.start()]
    rest = "" if section_match is None else text[section_match.start() :]

    new_model_line = f'model = "{_toml_quote(model)}"'
    new_provider_line = f'model_provider = "{_toml_quote(provider)}"'

    if re.search(r"^model\s*=", prefix, re.MULTILINE):
        prefix = re.sub(
            r'^model\s*=\s*"[^"]*"',
            lambda _m: new_model_line,
            prefix,
            count=1,
            flags=re.MULTILINE,
        )
    else:
        prefix = f"{new_model_line}\n{prefix}"

    if re.search(r"^model_provider\s*=", prefix, re.MULTILINE):
        prefix = re.sub(
            r'^model_provider\s*=\s*"[^"]*"',
            lambda _m: new_provider_line,
            prefix,
            count=1,
            flags=re.MULTILINE,
        )
    else:
        prefix = f"{new_provider_line}\n{prefix}"

    return prefix + rest


def _remove_managed_codexproxy_block(text: str) -> str:
    """Strip any pre-existing managed ``codexproxy`` tables and their markers.

    The launcher owns two TOML tables — ``[model_providers.codexproxy]`` (with
    its sub-tables like ``[model_providers.codexproxy.env]``) and the custom
    ``[codexproxy]`` table. The Codex CLI tolerates a lenient merge when the
    same table is declared twice, but strict TOML parsers (Python's
    ``tomllib``, the ``@iarna/toml`` JS package, etc.) reject it. To keep the
    file parseable in both worlds, we make sure there is exactly one managed
    block at the bottom of the file: if any of these tables already appear
    (whether or not the user kept the launcher's previous markers), we delete
    the section in place before re-appending the new block.
    """

    managed_roots = ("model_providers.codexproxy", "codexproxy")
    lines = text.splitlines(keepends=True)
    output: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            inner = stripped.strip("[]").strip()
            normalized = re.sub(r"\s+", "", inner)
            if any(
                normalized == root or normalized.startswith(root + ".")
                for root in managed_roots
            ):
                skipping = True
                continue
            skipping = False
        if not skipping:
            if stripped == "# >>> codexproxy (managed by cdx-codex) >>>":
                continue
            if stripped == "# <<< codexproxy <<<":
                continue
            output.append(line)
    return "".join(output)


def _write_codex_config(
    config_path: Path, *, base_url: str, api_key: str, model: str
) -> None:
    """Write or update ``config.toml`` so Codex CLI targets this proxy.

    The Codex CLI reads ``openai_base_url`` and ``api_key`` from the
    ``[model_providers.codexproxy]`` table; the top-level ``model_provider`` and
    ``model`` keys select that provider. We never delete pre-existing user
    settings — we only update the ``codexproxy`` block and the top-level
    ``model`` / ``model_provider`` keys. If the file already has a
    ``[model_providers.codexproxy]`` table (from a previous run or the user's
    own setup), we strip it before re-appending the managed block so the file
    remains strictly parseable.

    On the first call against a config that does not yet carry a managed
    CodexProxy block, the existing file is copied to
    ``<config_path>.codexproxy-backup`` so the user can later restore their
    pre-CodexProxy setup via ``cdx-restore``. The backup is only taken
    once — subsequent runs leave it untouched.
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing = ""
    if config_path.is_file():
        try:
            existing = config_path.read_text(encoding="utf-8")
        except OSError:
            existing = ""

    backup_path = _codex_config_backup_path(config_path)
    if (
        existing
        and not backup_path.exists()
        and "# >>> codexproxy (managed by cdx-codex) >>>" not in existing
    ):
        with contextlib.suppress(OSError):
            backup_path.write_text(existing, encoding="utf-8")

    deduped = _remove_managed_codexproxy_block(existing)
    deduped = _update_top_level_codex_settings(
        deduped, model=model, provider="codexproxy"
    )

    block = (
        "\n# >>> codexproxy (managed by cdx-codex) >>>\n"
        "[model_providers.codexproxy]\n"
        f'name = "codexproxy"\n'
        f'base_url = "{_toml_quote(base_url)}"\n'
        f'api_key = "{_toml_quote(api_key)}"\n'
        'wire_api = "responses"\n'
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
    merged = deduped.rstrip() + "\n" + block if deduped.strip() else block

    config_path.write_text(merged, encoding="utf-8")


def _clear_user_env_var(name: str) -> bool:
    """Remove ``name`` from the user-level environment. Returns True on success."""
    try:
        import winreg
    except ImportError:
        return False
    try:
        with (
            winreg.ConnectRegistry(None, winreg.HKEY_CURRENT_USER) as hive,
            winreg.OpenKey(
                hive, r"Environment", 0, winreg.KEY_SET_VALUE | winreg.KEY_READ
            ) as key,
            contextlib.suppress(FileNotFoundError),
        ):
            winreg.DeleteValue(key, name)
        return True
    except OSError:
        return False


def restore_codex_defaults() -> dict[str, Any]:
    """Restore the user's pre-CodexProxy ``config.toml`` and ``auth.json``.

    On Windows the user-level ``OPENAI_BASE_URL`` and ``OPENAI_API_KEY`` env
    vars (which Codex CLI v0.136+ treats as a higher-priority override of
    ``config.toml``) are also cleared so a future ``codex exec`` reads the
    restored ``config.toml`` and ``auth.json`` instead of pointing at the
    proxy or the wrong API key.

    Returns a status dict describing what was restored. If no backup exists
    for either file, that file is left untouched and a warning is recorded.
    """
    config_path = _codex_config_path_alt()
    backup_path = _codex_config_backup_path(config_path)
    legacy_backup = config_path.with_name(f"{config_path.name}.backup_pre_cdx")
    auth_path = _codex_auth_json_path()
    auth_backup = _codex_auth_json_backup_path(auth_path)

    restored: list[str] = []
    skipped: list[str] = []
    cleared_env: list[str] = []

    source = None
    if backup_path.is_file():
        source = backup_path
    elif legacy_backup.is_file():
        source = legacy_backup
    if source is not None:
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            restored.append(f"config: {config_path} (from {source.name})")
        except OSError as exc:
            skipped.append(f"config: {exc.__class__.__name__}")
    else:
        skipped.append(
            f"config: no backup found at {backup_path.name} or {legacy_backup.name}"
        )

    if auth_backup.is_file():
        try:
            auth_path.parent.mkdir(parents=True, exist_ok=True)
            auth_path.write_text(
                auth_backup.read_text(encoding="utf-8"), encoding="utf-8"
            )
            restored.append(f"auth: {auth_path} (from {auth_backup.name})")
        except OSError as exc:
            skipped.append(f"auth: {exc.__class__.__name__}")
    else:
        skipped.append("auth: no backup found")

    cleared_env.extend(
        var for var in ("OPENAI_BASE_URL", "OPENAI_API_KEY") if _clear_user_env_var(var)
    )

    return {
        "restored": restored,
        "skipped": skipped,
        "cleared_env": cleared_env,
        "config_path": str(config_path),
        "auth_path": str(auth_path),
    }


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


def _configure_codex_for_api(
    skip_preflight: bool = False,
) -> dict[str, str]:
    """Write ``~/.codex/config.toml`` so Codex targets this proxy. Raises on error.

    Set *skip_preflight* to ``True`` when called from inside the running proxy
    server (avoids a synchronous ``urlopen`` deadlock against the asyncio loop).

    Returns a dict with ``base_url``, ``api_key``, ``model``, ``provider`` on success.
    """
    settings = get_settings()
    proxy_root_url = local_proxy_root_url(settings)
    if not skip_preflight and (error := _preflight_proxy(proxy_root_url)):
        raise RuntimeError(f"CodexProxy is not reachable at {proxy_root_url}: {error}")

    token = settings.effective_auth_token or "freecc"
    config_path = _codex_config_path_alt()
    model = _default_codex_model(settings)
    _write_codex_config(
        config_path,
        base_url=f"{proxy_root_url.rstrip('/')}/v1",
        api_key=token,
        model=model,
    )
    return {
        "base_url": f"{proxy_root_url.rstrip('/')}/v1",
        "api_key": token,
        "model": model,
        "provider": "codexproxy",
    }


def configure_codex() -> None:
    """Write ``~/.codex/config.toml`` so Codex targets this proxy and exit.

    This is the Codex Desktop App entry point: it updates the same config file
    that ``codex exec`` reads but does not launch any child process. Use it
    when the Codex Desktop app is already running and you want it to start
    routing through this proxy. The app-server refreshes its config on every
    conversation, so the change takes effect on the next turn.
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
    model = _default_codex_model(settings)
    _write_codex_config(
        config_path,
        base_url=f"{proxy_root_url.rstrip('/')}/v1",
        api_key=token,
        model=model,
    )
    print(f"Codex CLI config written to {config_path}")
    print(f"  base_url  = {proxy_root_url.rstrip('/')}/v1")
    print(f"  api_key   = {token}")
    print(f"  model     = {model}")
    print("  provider  = codexproxy")
    print(
        "Launch (or restart) the Codex Desktop app to start using this proxy.",
    )


def launch_codex_app(argv: Sequence[str] | None = None) -> None:
    """Launch the OpenAI Codex Desktop App through this proxy.

    Writes ``~/.codex/config.toml`` with ``wire_api = "responses"``,
    ``openai_base_url`` pointing at the running proxy, and the configured
    auth token as ``OPENAI_API_KEY``, then exec's the ``codex app`` command.
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
    print(f"Codex Desktop App config written to {config_path}", file=sys.stderr)

    args = list(sys.argv[1:] if argv is None else argv)
    codex_command = shutil.which(settings.codex_cli_bin)
    if codex_command is None:
        print(
            f"Could not find Codex command: {settings.codex_cli_bin}",
            file=sys.stderr,
        )
        print(
            "Install Codex from https://github.com/openai/codex",
            file=sys.stderr,
        )
        raise SystemExit(127)

    command = [codex_command, "app", *args]
    env = _codex_child_env(settings, os.environ)
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(command, env=env)
        if process.pid:
            register_pid(process.pid)
        return_code = process.wait()
    except FileNotFoundError:
        print(
            f"Could not find Codex command: {settings.codex_cli_bin}",
            file=sys.stderr,
        )
        print(
            "Install Codex from https://github.com/openai/codex",
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


def delete_all() -> None:
    """Completely remove all CodexProxy files and configuration.

    Deletes:
    - ~/.codexproxy/ (entire config directory)
    - ~/.codex/config.toml.codexproxy-backup (if exists)
    - ~/.codex/auth.json.codexproxy-backup (if exists)
    - Clears environment variables: OPENAI_BASE_URL, OPENAI_API_KEY (Windows only)

    This is a destructive operation and cannot be undone.
    """

    config_dir = config_dir_path()
    config_path = _codex_config_path_alt()
    backup_path = _codex_config_backup_path(config_path)
    auth_path = _codex_auth_json_path()
    auth_backup = _codex_auth_json_backup_path(auth_path)

    deleted: list[str] = []
    failed: list[str] = []
    cleared_env: list[str] = []

    # Delete ~/.codexproxy/ directory
    if config_dir.exists():
        try:
            shutil.rmtree(config_dir)
            deleted.append(f"directory: {config_dir}")
        except OSError as exc:
            failed.append(f"directory: {config_dir} ({exc.__class__.__name__})")

    # Delete config backup
    if backup_path.exists():
        try:
            backup_path.unlink()
            deleted.append(f"config backup: {backup_path}")
        except OSError as exc:
            failed.append(f"config backup: {backup_path} ({exc.__class__.__name__})")

    # Delete auth backup
    if auth_backup.exists():
        try:
            auth_backup.unlink()
            deleted.append(f"auth backup: {auth_backup}")
        except OSError as exc:
            failed.append(f"auth backup: {auth_backup} ({exc.__class__.__name__})")

    # Clear Windows environment variables
    cleared_env.extend(
        var
        for var in ("OPENAI_BASE_URL", "OPENAI_API_KEY")
        if _clear_user_env_var(var)
    )

    # Print summary
    print("CodexProxy deletion summary:")
    if deleted:
        print("\nDeleted:")
        for item in deleted:
            print(f"  ✓ {item}")
    if cleared_env:
        print("\nCleared environment variables:")
        for var in cleared_env:
            print(f"  ✓ {var}")
    if failed:
        print("\nFailed to delete:")
        for item in failed:
            print(f"  ✗ {item}")

    if not deleted and not cleared_env:
        print("  (nothing to delete)")

    if failed:
        raise SystemExit(1)


def restore() -> None:
    """Restore the user's pre-CodexProxy configuration (registered as `cdx-restore`).

    Restores:
    - ~/.codex/config.toml from backup
    - ~/.codex/auth.json from backup
    - Clears OPENAI_BASE_URL and OPENAI_API_KEY (Windows only)

    This reverses the effects of ``cdx-codex`` and ``cdx-codex-app``.
    """

    result = restore_codex_defaults()

    print("CodexProxy restoration summary:")
    if result["restored"]:
        print("\nRestored:")
        for item in result["restored"]:
            print(f"  ✓ {item}")
    if result["cleared_env"]:
        print("\nCleared environment variables:")
        for var in result["cleared_env"]:
            print(f"  ✓ {var}")
    if result["skipped"]:
        print("\nSkipped (not found):")
        for item in result["skipped"]:
            print(f"  ℹ {item}")


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
