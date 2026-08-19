"""Lightweight entry points for installed CodexProxy commands."""

import contextlib
import json
import os
import re
import shutil
import sys
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from codexproxy.cli.launchers.common import preflight_proxy
from codexproxy.core.version import package_version


def serve(argv: Sequence[str] | None = None) -> None:
    """Start the FastAPI server (registered as ``cdx-server``)."""
    if _print_version_if_requested(argv):
        return

    # Keep the server composition root off metadata-only command paths.
    from codexproxy.cli.commands import serve as run_server

    run_server()


def _print_version_if_requested(argv: Sequence[str] | None) -> bool:
    args = sys.argv[1:] if argv is None else argv
    if "--version" not in args:
        return False
    print(f"codexproxy {package_version()}")
    return True


# ===== Codex CLI launcher functions =====

def launch_codex(argv: Sequence[str] | None = None) -> None:
    """Launch the OpenAI Codex CLI through this proxy (registered as ``cdx-codex``)."""
    from codexproxy.cli.launchers.codex import launch as _launch
    _launch(argv)


def launch_codex_app(argv: Sequence[str] | None = None) -> None:
    """Launch the OpenAI Codex Desktop App through this proxy (registered as ``cdx-codex-app``)."""
    from codexproxy.cli.launchers.codex import launch as _launch
    # The Codex Desktop App uses the same launcher as the CLI
    _launch(argv)


def write_codex_config_only(argv: Sequence[str] | None = None) -> None:
    """Write Codex config only (for Desktop App) without launching (registered as ``cdx-codex-config``)."""
    import sys
    from codexproxy.config.loader import get_settings
    from codexproxy.config.server_urls import local_proxy_root_url
    from codexproxy.cli.launchers.codex import (
        build_codex_launcher_env,
        build_codex_launcher_command,
        codex_model_catalog_config_args,
    )
    from codexproxy.config.loader import get_settings
    from codexproxy.config.server_urls import local_proxy_root_url

    if "--version" in (sys.argv[1:] if argv is None else argv):
        from codexproxy.core.version import package_version
        print(f"codexproxy {package_version()}")
        return

    settings = get_settings()
    proxy_root_url = local_proxy_root_url(settings)
    if error := _preflight_proxy(proxy_root_url):
        print(
            f"CodexProxy proxy is not reachable at {proxy_root_url}: {error}",
            file=sys.stderr,
        )
        print("Start it in another terminal with: cdx-server", file=sys.stderr)
        raise SystemExit(1)

    settings = get_settings()
    proxy_root_url = local_proxy_root_url(settings)

    # Generate model catalog
    catalog_args = _codex_model_catalog_config_args(proxy_root_url, settings)

    # Build the config args
    from codexproxy.cli.launchers.codex import (
        build_codex_launcher_env,
        build_codex_launcher_command,
        codex_model_catalog_config_args,
        codex_config_args,
        build_codex_launcher_env,
    )
    from codexproxy.cli.launchers.common import proxy_v1_url
    from codexproxy.config.settings import Settings

    settings = get_settings()
    proxy_root_url = local_proxy_root_url(settings)
    api_url = _ensure_v1_url(proxy_root_url)

    # Generate model catalog
    catalog_args = codex_model_catalog_config_args(proxy_root_url, settings)

    # Write the config file only
    config_path = _codex_config_path()
    _write_codex_config(config_path, base_url=api_url, api_key="cdx-codex", model=settings.model or "gpt-4o")

    print(f"Codex config written to {config_path}")


# ===== Config management utilities =====

def _preflight_proxy(proxy_root_url: str) -> str | None:
    """Return an error message when the local proxy health check is unreachable."""
    from codexproxy.cli.launchers.common import preflight_proxy
    return preflight_proxy(proxy_root_url)


def _ensure_v1_url(url: str) -> str:
    stripped = url.rstrip("/")
    return stripped if stripped.endswith("/v1") else f"{stripped}/v1"


def _toml_assignment(key: str, value: str | list[str]) -> str:
    if isinstance(value, list):
        return f"{key}={json.dumps(value)}"
    return f"{key}={json.dumps(value)}"


def _toml_assignment_escaped(key: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key} = "{escaped}"'


def _codex_config_path() -> Path:
    """Return the path to the Codex config file."""
    return Path.home() / ".codex" / "config.toml"


def _codex_config_backup_path(config_path: Path) -> Path:
    return config_path.with_name(f"{config_path.name}.codexproxy-backup")


def _codex_auth_json_path() -> Path:
    return Path.home() / ".codex" / "auth.json"


def _codex_auth_json_backup_path(auth_path: Path) -> Path:
    return auth_path.with_name(f"{auth_path.name}.codexproxy-backup")


def _codex_config_backup_path_alt(config_path: Path) -> Path:
    return config_path.with_name(f"{config_path.name}.backup_pre_cdx")


def _codex_config_path_alt() -> Path:
    return Path.home() / ".codex" / "config.toml"


def _codex_auth_json_path_alt() -> Path:
    return Path.home() / ".codex" / "auth.json"


def _codex_auth_json_backup_path_alt(auth_path: Path) -> Path:
    return auth_path.with_name(f"{auth_path.name}.codexproxy-backup")


def _toml_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _remove_managed_codexproxy_block(text: str) -> str:
    """Strip any pre-existing managed ``codexproxy`` tables and their markers."""
    managed_roots = ("model_providers.cdx", "cdx")
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


def _update_top_level_codex_settings(text: str, *, model: str, provider: str) -> str:
    """Update top-level ``model`` and ``model_provider`` in a Codex config file."""
    section_match = re.search(r"^\s*\[", text, re.MULTILINE)
    prefix = text if section_match is None else text[: section_match.start()]
    rest = "" if section_match is None else text[section_match.start():]

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


def _write_codex_config(
    config_path: Path, *, base_url: str, api_key: str, model: str
) -> None:
    """Write or update ``config.toml`` so Codex CLI targets this proxy."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing = ""
    if config_path.is_file():
        try:
            existing = config_path.read_text(encoding="utf-8")
        except OSError:
            existing = ""

    backup_path = _codex_config_backup_path(Path(config_path))
    if (
        existing
        and not backup_path.exists()
        and "# >>> codexproxy (managed by cdx-codex) >>>" not in existing
    ):
        with contextlib.suppress(OSError):
            backup_path.write_text(existing, encoding="utf-8")

    deduped = _remove_managed_codexproxy_block(existing)
    deduped = _update_top_level_codex_settings(
        deduped, model=model, provider="cdx"
    )

    block = (
        "\n# >>> codexproxy (managed by cdx-codex) >>>\n"
        "[model_providers.cdx]\n"
        f'name = "CodexProxy"\n'
        f'base_url = "{_toml_quote(base_url)}"\n'
        f'api_key = "{_toml_quote(api_key)}"\n'
        'wire_api = "responses"\n'
        "\n"
        "[model_providers.cdx.env]\n"
        f'OPENAI_API_KEY = "{_toml_quote(api_key)}"\n'
        "\n"
        "[cdx]\n"
        f'model = "{_toml_quote(model)}"\n'
        f'model_provider = "cdx"\n'
        'approval_policy = "never"\n'
        'sandbox_mode = "workspace-write"\n'
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


def restore_codex_defaults(argv: Sequence[str] | None = None) -> None:
    """Restore the user's pre-CodexProxy ``config.toml`` and ``auth.json`` (registered as ``cdx-restore``)."""
    if "--version" in (sys.argv[1:] if argv is None else argv):
        from codexproxy.core.version import package_version
        print(f"codexproxy {package_version()}")
        return

    config_path = _codex_config_path()
    backup_path = _codex_config_backup_path(Path(config_path))
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

    print("Restored:")
    for item in restored:
        print(f"  ✓ {item}")
    if skipped:
        print("Skipped:")
        for item in skipped:
            print(f"  ⚠ {item}")
    if cleared_env:
        print("Cleared user env vars:")
        for var in cleared_env:
            print(f"  ✓ {var}")


def delete_codexproxy_data(argv: Sequence[str] | None = None) -> None:
    """Complete removal of all CodexProxy files (registered as ``cdx-delete``)."""
    if "--version" in (sys.argv[1:] if argv is None else argv):
        from codexproxy.core.version import package_version
        print(f"codexproxy {package_version()}")
        return

    if "--yes" not in (sys.argv[1:] if argv is None else argv):
        confirm = input(
            "This will permanently delete all CodexProxy config, backups, and data. Continue? [y/N]: "
        )
        if confirm.lower() != "y":
            print("Aborted.")
            return

    # Remove config files
    config_path = _codex_config_path()
    backup_path = _codex_config_backup_path(Path(config_path))
    legacy_backup = config_path.with_name(f"{config_path.name}.backup_pre_cdx")
    auth_path = _codex_auth_json_path()
    auth_backup = _codex_auth_json_backup_path(auth_path)

    deleted: list[str] = []
    for path in [config_path, backup_path, legacy_backup, auth_path, _codex_auth_json_backup_path(auth_path)]:
        if path.exists():
            try:
                path.unlink()
                deleted.append(str(path))
            except OSError as exc:
                print(f"Failed to delete {path}: {exc}", file=sys.stderr)

    # Remove .cdx directory
    cdx_dir = Path.home() / ".cdx"
    if cdx_dir.exists():
        try:
            shutil.rmtree(cdx_dir)
            deleted.append(str(cdx_dir))
        except OSError as exc:
            print(f"Failed to delete {cdx_dir}: {exc}", file=sys.stderr)

    # Clear user env vars
    cleared_env = [
        var for var in ("OPENAI_BASE_URL", "OPENAI_API_KEY") if _clear_user_env_var(var)
    ]

    print("Deleted:")
    for item in deleted:
        print(f"  ✓ {item}")
    if cleared_env:
        print("Cleared user env vars:")
        for var in cleared_env:
            print(f"  ✓ {var}")
    print("CodexProxy data removed.")


def init_codexproxy(argv: Sequence[str] | None = None) -> None:
    """Optional scaffold for advanced setup (registered as ``cdx-init``)."""
    if "--version" in (sys.argv[1:] if argv is None else argv):
        from codexproxy.core.version import package_version
        print(f"codexproxy {package_version()}")
        return

    from codexproxy.config.loader import get_settings
    from codexproxy.config.server_urls import local_proxy_root_url
    from codexproxy.cli.launchers.codex import codex_model_catalog_config_args

    settings = get_settings()
    proxy_root_url = local_proxy_root_url(settings)

    print("CodexProxy initialization scaffold")
    print(f"Proxy URL: {proxy_root_url}")

    # Generate model catalog
    try:
        catalog_args = _codex_model_catalog_config_args(proxy_root_url, get_settings())
        print(f"Model catalog generated: {len(catalog_args)} config args")
    except Exception as exc:
        print(f"Warning: could not generate model catalog: {exc}")

    # Write initial config
    config_path = _codex_config_path()
    _write_codex_config(config_path, base_url=local_proxy_root_url(settings), api_key="cdx-init", model="gpt-4o")
    print(f"Initial config written to {config_path}")

    print("\nCodexProxy initialized. Run 'cdx-server' to start the proxy.")


def _codex_model_catalog_config_args(proxy_root_url: str, settings) -> list[str]:
    """Prepare the generated Codex model catalog and return its config args."""
    from codexproxy.cli.launchers.codex import (
        fetch_proxy_models_response,
        build_codex_model_catalog,
        write_codex_model_catalog,
        codex_model_catalog_path,
        build_model_catalog_config_args,
    )

    try:
        models_response = fetch_proxy_models_response(
            proxy_root_url, settings.proxy_auth_token
        )
        catalog = build_codex_model_catalog(models_response)
        models = catalog.get("models")
        if not isinstance(models, list) or not models:
            print(
                "CodexProxy warning: Codex model catalog is empty; "
                "launching without model picker catalog.",
                file=sys.stderr,
            )
            return []
        catalog_path = codex_model_catalog_path()
        write_codex_model_catalog(catalog_path, catalog)
    except Exception as exc:
        print(
            "CodexProxy warning: could not prepare Codex model catalog "
            f"({exc}); launching without model picker catalog.",
            file=sys.stderr,
        )
        return []

    return build_model_catalog_config_args(str(catalog_path))


def _ensure_v1_url(url: str) -> str:
    stripped = url.rstrip("/")
    return stripped if stripped.endswith("/v1") else f"{stripped}/v1"