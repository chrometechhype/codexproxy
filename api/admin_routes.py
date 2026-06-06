"""Local admin UI routes and APIs."""

from __future__ import annotations

import inspect
import ipaddress
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from cli.process_registry import register_pid
from config.settings import Settings
from config.settings import get_settings as get_cached_settings
from providers.registry import ProviderRegistry

from .admin_config import (
    FIELD_BY_KEY,
    load_config_response,
    provider_config_status,
    validate_updates,
    write_managed_env,
)
from .admin_urls import local_admin_url, local_proxy_root_url

router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parent / "admin_static"
LOCAL_PROVIDER_PATHS = {
    "lmstudio": "/models",
    "llamacpp": "/models",
    "ollama": "/api/tags",
}


class AdminConfigPayload(BaseModel):
    """Partial config update submitted by the admin UI."""

    values: dict[str, Any] = Field(default_factory=dict)


def _is_loopback_host(host: str | None) -> bool:
    if host is None:
        return False
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _origin_is_local(origin: str | None) -> bool:
    if not origin:
        return True
    parsed = urlsplit(origin)
    return _is_loopback_host(parsed.hostname)


def require_loopback_admin(request: Request) -> None:
    """Allow admin access only from the local machine."""

    client_host = request.client.host if request.client else None
    if not _is_loopback_host(client_host):
        raise HTTPException(status_code=403, detail="Admin UI is local-only")

    origin = request.headers.get("origin")
    if not _origin_is_local(origin):
        raise HTTPException(status_code=403, detail="Admin UI is local-only")


def _asset_response(filename: str) -> FileResponse:
    path = STATIC_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Admin asset not found")
    return FileResponse(path)


@router.get("/admin", include_in_schema=False)
async def admin_page(request: Request):
    require_loopback_admin(request)
    return _asset_response("index.html")


@router.get("/admin/assets/{filename}", include_in_schema=False)
async def admin_asset(filename: str, request: Request):
    require_loopback_admin(request)
    if filename not in {"admin.css", "admin.js"}:
        raise HTTPException(status_code=404, detail="Admin asset not found")
    return _asset_response(filename)


@router.get("/admin/api/config")
async def get_admin_config(request: Request):
    require_loopback_admin(request)
    return load_config_response()


@router.post("/admin/api/config/validate")
async def validate_admin_config(payload: AdminConfigPayload, request: Request):
    require_loopback_admin(request)
    return validate_updates(_filtered_values(payload.values))


@router.post("/admin/api/config/apply")
async def apply_admin_config(
    payload: AdminConfigPayload,
    request: Request,
    background_tasks: BackgroundTasks,
):
    require_loopback_admin(request)
    result = write_managed_env(_filtered_values(payload.values))
    if not result["applied"]:
        return result

    get_cached_settings.cache_clear()
    restart = _restart_metadata(result["pending_fields"], request)
    result["restart"] = restart
    if restart["required"] and restart["automatic"]:
        callback = request.app.state.admin_restart_callback
        background_tasks.add_task(_invoke_admin_restart_callback, callback)
        request.app.state.admin_pending_fields = []
        return result

    old_registry = getattr(request.app.state, "provider_registry", None)
    if isinstance(old_registry, ProviderRegistry):
        await old_registry.cleanup()
    request.app.state.provider_registry = ProviderRegistry()
    request.app.state.admin_pending_fields = result["pending_fields"]
    return result


@router.get("/admin/api/status")
async def admin_status(request: Request):
    require_loopback_admin(request)
    settings = get_cached_settings()
    registry = getattr(request.app.state, "provider_registry", None)
    cached_models: dict[str, list[str]] = {}
    if isinstance(registry, ProviderRegistry):
        cached_models = {
            provider_id: sorted(model_ids)
            for provider_id, model_ids in registry.cached_model_ids().items()
        }
    return {
        "status": "running",
        "host": settings.host,
        "port": settings.port,
        "model": settings.model,
        "provider": settings.provider_type,
        "pending_fields": getattr(request.app.state, "admin_pending_fields", []),
        "provider_status": provider_config_status(),
        "cached_models": cached_models,
    }


@router.get("/admin/api/providers/local-status")
async def local_provider_status(request: Request):
    require_loopback_admin(request)
    config = load_config_response()
    values = {field["key"]: field["value"] for field in config["fields"]}
    checks = []
    for provider_id, path in LOCAL_PROVIDER_PATHS.items():
        base_url = _local_provider_url(provider_id, values)
        checks.append(await _check_local_provider(provider_id, base_url, path))
    return {"providers": checks}


@router.post("/admin/api/providers/{provider_id}/test")
async def test_provider(provider_id: str, request: Request):
    require_loopback_admin(request)
    settings = get_cached_settings()
    registry = getattr(request.app.state, "provider_registry", None)
    if not isinstance(registry, ProviderRegistry):
        registry = ProviderRegistry()
        request.app.state.provider_registry = registry
    try:
        provider = registry.get(provider_id, settings)
        infos = await provider.list_model_infos()
    except Exception as exc:
        return {
            "provider_id": provider_id,
            "ok": False,
            "error_type": type(exc).__name__,
        }
    registry.cache_model_infos(provider_id, infos)
    return {
        "provider_id": provider_id,
        "ok": True,
        "models": sorted(info.model_id for info in infos),
    }


@router.post("/admin/api/models/refresh")
async def refresh_models(request: Request):
    require_loopback_admin(request)
    settings = get_cached_settings()
    registry = getattr(request.app.state, "provider_registry", None)
    if not isinstance(registry, ProviderRegistry):
        registry = ProviderRegistry()
        request.app.state.provider_registry = registry
    await registry.refresh_model_list_cache(settings)
    return {
        "cached_models": {
            provider_id: sorted(model_ids)
            for provider_id, model_ids in registry.cached_model_ids().items()
        }
    }


@router.get("/admin/api/codex/status")
async def codex_status(request: Request):
    """Return the current Codex integration state for the admin UI."""
    require_loopback_admin(request)
    from cli.entrypoints import (
        _codex_config_backup_path,
        _codex_config_path_alt,
    )

    config_path = _codex_config_path_alt()
    backup_path = _codex_config_backup_path(config_path)
    legacy_backup = config_path.with_name(f"{config_path.name}.backup_pre_cdx")
    proxy_url = local_proxy_root_url(get_cached_settings())
    return {
        "proxy_url": proxy_url,
        "config_path": str(config_path),
        "config_exists": config_path.is_file(),
        "backup_path": str(backup_path),
        "backup_exists": backup_path.is_file(),
        "legacy_backup_exists": legacy_backup.is_file(),
        "codex_cli_available": shutil.which("codex") is not None,
        "codex_app_installed": _codex_app_install_path() is not None,
        "codex_app_path": _codex_app_install_path(),
    }


@router.post("/admin/api/codex/launch-cli")
async def codex_launch_cli(request: Request):
    """Configure proxy, then spawn ``codex`` interactive TUI in a new console."""
    require_loopback_admin(request)
    codex_path = shutil.which("codex")
    if codex_path is None:
        raise HTTPException(status_code=404, detail="Codex CLI not found on PATH")
    flags = await _flags_from_launch_request(request, default_flags=())
    try:
        from cli.entrypoints import _configure_codex_for_api

        config_info = _configure_codex_for_api(skip_preflight=True)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    env = {
        **os.environ,
        "OPENAI_BASE_URL": config_info["base_url"],
        "OPENAI_API_KEY": config_info["api_key"],
    }
    settings = get_cached_settings()
    proxy_url = local_proxy_root_url(settings)
    try:
        process = _spawn_with_new_console(
            [codex_path, *flags],
            env=env,
        )
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to launch: {exc}") from exc
    if process.pid:
        register_pid(process.pid)
    return {
        "pid": process.pid,
        "command": [codex_path, *flags],
        "proxy_url": proxy_url,
    }


@router.post("/admin/api/codex/launch-app")
async def codex_launch_app(request: Request):
    """Configure proxy in config.toml, then spawn the Codex Desktop App."""
    require_loopback_admin(request)
    app_path = _codex_app_install_path()
    if app_path is None:
        raise HTTPException(status_code=404, detail="Codex Desktop App not installed")
    try:
        from cli.entrypoints import _configure_codex_for_api

        _configure_codex_for_api(skip_preflight=True)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    settings = get_cached_settings()
    proxy_url = local_proxy_root_url(settings)
    try:
        if "WindowsApps" in app_path:
            aumid = _store_codex_aumid()
            if aumid is None:
                raise HTTPException(
                    status_code=500,
                    detail="Store app detected but AUMID not found",
                )
            process = _spawn_detached(["explorer.exe", f"shell:AppsFolder\\{aumid}"])
        else:
            process = _spawn_detached([str(app_path)])
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to launch: {exc}") from exc
    if process.pid:
        register_pid(process.pid)
    return {
        "pid": process.pid,
        "command": [str(app_path)],
        "proxy_url": proxy_url,
    }


@router.post("/admin/api/codex/restore-default")
async def codex_restore_default(request: Request):
    """Restore the user's pre-CodexProxy ``config.toml`` and ``auth.json``."""
    require_loopback_admin(request)
    from cli.entrypoints import restore_codex_defaults

    result = restore_codex_defaults()
    return result


def _filtered_values(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if key in FIELD_BY_KEY}


async def _invoke_admin_restart_callback(callback: Any) -> None:
    result = callback()
    if inspect.isawaitable(result):
        await result


def _restart_metadata(fields: list[str], request: Request) -> dict[str, Any]:
    callback = getattr(request.app.state, "admin_restart_callback", None)
    automatic = bool(fields and callable(callback))
    return {
        "required": bool(fields),
        "automatic": automatic,
        "admin_url": _next_admin_url() if automatic else None,
        "fields": fields,
    }


def _next_admin_url() -> str:
    fields = {
        field["key"]: field["value"] for field in load_config_response()["fields"]
    }
    settings = Settings.model_construct(
        host=fields.get("HOST") or "0.0.0.0",
        port=int(fields.get("PORT") or 8082),
    )
    return local_admin_url(settings)


def _local_provider_url(provider_id: str, values: dict[str, str]) -> str:
    if provider_id == "lmstudio":
        return values.get("LM_STUDIO_BASE_URL", "")
    if provider_id == "llamacpp":
        return values.get("LLAMACPP_BASE_URL", "")
    if provider_id == "ollama":
        return values.get("OLLAMA_BASE_URL", "")
    return ""


def _codex_app_install_path() -> str | None:
    """Return the path to the Codex Desktop App on Windows, or None."""
    if sys.platform != "win32":
        return shutil.which("codex-desktop") or shutil.which("Codex")
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Codex" / "Codex.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Codex" / "Codex.exe",
    ]
    programs = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    candidates.append(Path(programs) / "Codex" / "Codex.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    store_path = _find_store_codex_exe()
    if store_path is not None:
        return store_path
    fallback = shutil.which("Codex")
    if fallback and fallback.lower().endswith(".exe"):
        return fallback
    return None


_store_codex_cache: dict[str, str | None] = {}


def _find_store_codex_exe() -> str | None:
    """Detect Windows Store (AppX) Codex installation and return the .exe path."""
    cached = _store_codex_cache.get("exe")
    if cached is not None:
        return cached if cached else None
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "$pkg = Get-AppxPackage -Name '*Codex*';"
                "if ($pkg) { $pkg.InstallLocation }",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        install = result.stdout.strip()
        if install:
            exe = Path(install) / "app" / "Codex.exe"
            if exe.is_file():
                path = str(exe)
                _store_codex_cache["exe"] = path
                return path
    except Exception:
        pass
    _store_codex_cache["exe"] = ""
    return None


def _store_codex_aumid() -> str | None:
    """Return the AUMID for the Windows Store Codex app, or None."""
    cached = _store_codex_cache.get("aumid")
    if cached is not None:
        return cached if cached else None
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "$pkg = Get-AppxPackage -Name '*Codex*';"
                "if ($pkg) { $pkg.PackageFamilyName }",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        family = result.stdout.strip()
        if family:
            aumid = f"{family}!App"
            _store_codex_cache["aumid"] = aumid
            return aumid
    except Exception:
        pass
    _store_codex_cache["aumid"] = ""
    return None


def _spawn_with_new_console(
    args: list[str], env: dict[str, str] | None = None
) -> subprocess.Popen[bytes]:
    """Spawn a console application in a new console window (Windows-aware)."""
    if sys.platform == "win32":
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NEW_CONSOLE
        )
        return subprocess.Popen(
            args,
            close_fds=True,
            creationflags=creationflags,
            env=env,
        )
    return subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
        env=env,
    )


def _spawn_detached(
    args: list[str], env: dict[str, str] | None = None
) -> subprocess.Popen[bytes]:
    """Spawn a process detached from the current console (Windows-aware)."""
    if sys.platform == "win32":
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_BREAKAWAY_FROM_JOB
        )
        return subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
            env=env,
        )
    return subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
        env=env,
    )


async def _flags_from_launch_request(
    request: Request, *, default_flags: tuple[str, ...]
) -> list[str]:
    """Extract optional flags from the JSON body, falling back to ``default_flags``."""
    try:
        body = await request.json()
    except Exception:
        return list(default_flags)
    if not isinstance(body, dict):
        return list(default_flags)
    flags = body.get("flags")
    if isinstance(flags, list) and all(isinstance(flag, str) for flag in flags):
        return flags
    return list(default_flags)


async def _check_local_provider(
    provider_id: str, base_url: str, path: str
) -> dict[str, Any]:
    clean_url = base_url.strip().rstrip("/")
    if not clean_url:
        return {
            "provider_id": provider_id,
            "status": "missing_url",
            "label": "Missing URL",
            "base_url": base_url,
        }

    url = f"{clean_url}{path}"
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            response = await client.get(url)
        ok = 200 <= response.status_code < 300
        return {
            "provider_id": provider_id,
            "status": "reachable" if ok else "offline",
            "label": "Reachable" if ok else "Offline",
            "base_url": base_url,
            "status_code": response.status_code,
        }
    except Exception as exc:
        return {
            "provider_id": provider_id,
            "status": "offline",
            "label": "Offline",
            "base_url": base_url,
            "error_type": type(exc).__name__,
        }
