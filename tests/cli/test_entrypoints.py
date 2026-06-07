"""Tests for cli/entrypoints.py — cdx-init scaffolding logic."""

import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from config.settings import Settings


def _launcher_settings(
    *,
    port: int = 8083,
    token: str = "codexproxy",
) -> Settings:
    return Settings.model_construct(
        host="0.0.0.0",
        port=port,
        anthropic_auth_token=token,
    )


def _run_init(tmp_home: Path) -> tuple[str, Path]:
    """Run init() with home directory redirected to tmp_home. Returns (printed output, env_file path)."""
    from cli.entrypoints import init

    env_file = tmp_home / ".codexproxy" / ".env"
    printed: list[str] = []

    with (
        patch("pathlib.Path.home", return_value=tmp_home),
        patch(
            "builtins.print",
            side_effect=lambda *a: printed.append(" ".join(str(x) for x in a)),
        ),
    ):
        init()

    return "\n".join(printed), env_file


def test_init_creates_env_file(tmp_path: Path) -> None:
    """init() creates .env from the bundled template when it doesn't exist yet."""
    output, env_file = _run_init(tmp_path)

    assert env_file.exists()
    assert env_file.stat().st_size > 0
    assert str(env_file) in output


def test_init_copies_template_content(tmp_path: Path) -> None:
    """init() writes the canonical root env.example content, not an empty file."""
    template = (Path(__file__).resolve().parents[2] / ".env.example").read_text(
        encoding="utf-8"
    )
    _, env_file = _run_init(tmp_path)

    assert env_file.read_text("utf-8") == template


def test_init_migrates_home_checkout_env_before_template(tmp_path: Path) -> None:
    """init() preserves users who kept config in ~/codexproxy/.env."""
    legacy_env = tmp_path / "codexproxy" / ".env"
    legacy_env.parent.mkdir(parents=True)
    legacy_env.write_text("MODEL=deepseek/deepseek-chat\n", encoding="utf-8")

    output, env_file = _run_init(tmp_path)

    assert env_file.read_text("utf-8") == "MODEL=deepseek/deepseek-chat\n"
    assert f"Config migrated from {legacy_env}" in output


def test_init_migrates_legacy_xdg_env_before_template(tmp_path: Path) -> None:
    """init() preserves users who kept config in ~/.config/codexproxy/.env."""
    legacy_env = tmp_path / ".config" / "codexproxy" / ".env"
    legacy_env.parent.mkdir(parents=True)
    legacy_env.write_text("MODEL=open_router/free-model\n", encoding="utf-8")

    output, env_file = _run_init(tmp_path)

    assert env_file.read_text("utf-8") == "MODEL=open_router/free-model\n"
    assert f"Config migrated from {legacy_env}" in output


def test_init_migrates_legacy_cdx_home_env_before_template(tmp_path: Path) -> None:
    """init() preserves users who kept config in ~/.cdx/.env."""
    legacy_env = tmp_path / ".cdx" / ".env"
    legacy_env.parent.mkdir(parents=True)
    legacy_env.write_text("MODEL=groq/legacy\n", encoding="utf-8")

    output, env_file = _run_init(tmp_path)

    assert env_file.read_text("utf-8") == "MODEL=groq/legacy\n"
    assert f"Config migrated from {legacy_env}" in output


def test_legacy_env_migration_does_not_overwrite_managed_env(
    tmp_path: Path,
) -> None:
    """Legacy migration never overwrites an existing ~/.codexproxy/.env."""
    from cli.entrypoints import _migrate_legacy_env_if_missing

    managed_env = tmp_path / ".codexproxy" / ".env"
    managed_env.parent.mkdir(parents=True)
    managed_env.write_text("MODEL=nvidia_nim/current\n", encoding="utf-8")
    legacy_env = tmp_path / "codexproxy" / ".env"
    legacy_env.parent.mkdir(parents=True)
    legacy_env.write_text("MODEL=deepseek/legacy\n", encoding="utf-8")

    with patch("pathlib.Path.home", return_value=tmp_path):
        migrated_from = _migrate_legacy_env_if_missing()

    assert migrated_from is None
    assert managed_env.read_text("utf-8") == "MODEL=nvidia_nim/current\n"


def test_env_template_loader_uses_root_template_in_source_checkout() -> None:
    """Source checkout fallback uses the root .env.example as the single source."""
    from cli.entrypoints import _load_env_template

    template = (Path(__file__).resolve().parents[2] / ".env.example").read_text(
        encoding="utf-8"
    )

    assert _load_env_template() == template


def test_init_creates_parent_directories(tmp_path: Path) -> None:
    """init() creates ~/.codexproxy/ even if it doesn't exist."""
    config_dir = tmp_path / ".codexproxy"
    assert not config_dir.exists()

    _run_init(tmp_path)

    assert config_dir.is_dir()


def test_init_skips_if_env_already_exists(tmp_path: Path) -> None:
    """init() does not overwrite an existing .env and prints a warning."""
    # Create it first
    _run_init(tmp_path)

    env_file = tmp_path / ".codexproxy" / ".env"
    env_file.write_text("existing content", encoding="utf-8")

    output, _ = _run_init(tmp_path)

    assert env_file.read_text("utf-8") == "existing content"
    assert "already exists" in output


def test_init_prints_next_step_hint(tmp_path: Path) -> None:
    """init() tells the user to run cdx-server after editing .env."""
    output, _ = _run_init(tmp_path)

    assert "cdx-server" in output


def test_cli_scripts_are_registered() -> None:
    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )

    scripts = pyproject["project"]["scripts"]
    assert scripts["cdx-server"] == "cli.entrypoints:serve"
    assert scripts["codexproxy"] == "cli.entrypoints:serve"
    assert scripts["cdx-codex"] == "cli.entrypoints:launch_codex"


def test_schedule_open_admin_browser_opens_when_health_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opening /admin runs after /health preflight succeeds."""
    monkeypatch.delenv("CDX_OPEN_BROWSER", raising=False)
    monkeypatch.delenv("CODEX_PROXY_OPEN_BROWSER", raising=False)
    from api.admin_urls import local_admin_url
    from cli import entrypoints

    settings = _launcher_settings(port=31337)
    opened_urls: list[str] = []

    class ImmediateThread:
        def __init__(self, target=None, **_kwargs: object) -> None:
            self._target = target

        def start(self) -> None:
            assert self._target is not None
            self._target()

    monkeypatch.setattr(entrypoints.threading, "Thread", ImmediateThread)

    with (
        patch("cli.entrypoints.webbrowser.open", side_effect=opened_urls.append),
        patch(
            "cli.entrypoints._preflight_proxy",
            return_value=None,
        ),
    ):
        entrypoints._schedule_open_admin_browser(settings)
        import time as _t

        _t.sleep(0.05)

    assert opened_urls == [local_admin_url(settings)]


def test_schedule_open_admin_browser_skips_when_preflight_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If /health never becomes reachable, the browser is not opened."""
    monkeypatch.delenv("CDX_OPEN_BROWSER", raising=False)
    monkeypatch.delenv("CODEX_PROXY_OPEN_BROWSER", raising=False)
    from cli import entrypoints

    opened: list[str] = []

    class ImmediateThread:
        def __init__(self, target=None, **_kwargs: object) -> None:
            self._target = target

        def start(self) -> None:
            assert self._target is not None
            self._target()

    monkeypatch.setattr(entrypoints.threading, "Thread", ImmediateThread)

    with (
        patch("cli.entrypoints.webbrowser.open", side_effect=opened.append),
        patch(
            "cli.entrypoints._preflight_proxy",
            return_value="timeout",
        ),
        patch("cli.entrypoints.time.sleep", return_value=None),
    ):
        entrypoints._schedule_open_admin_browser(_launcher_settings())

    assert opened == []


def test_admin_browser_open_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting CODEX_PROXY_OPEN_BROWSER=0 prevents the scheduler from running."""
    monkeypatch.setenv("CODEX_PROXY_OPEN_BROWSER", "0")
    from cli import entrypoints

    started: list[bool] = []

    class DummyThread:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._args = args
            self._kwargs = kwargs

        def start(self) -> None:
            started.append(True)

    monkeypatch.setattr(entrypoints.threading, "Thread", DummyThread)
    entrypoints._schedule_open_admin_browser(_launcher_settings())

    assert started == []


def test_admin_browser_open_respects_legacy_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy CDX_OPEN_BROWSER=0 still disables the scheduler (one-release alias)."""
    monkeypatch.delenv("CODEX_PROXY_OPEN_BROWSER", raising=False)
    monkeypatch.setenv("CDX_OPEN_BROWSER", "0")
    from cli import entrypoints

    started: list[bool] = []

    class DummyThread:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def start(self) -> None:
            started.append(True)

    monkeypatch.setattr(entrypoints.threading, "Thread", DummyThread)
    entrypoints._schedule_open_admin_browser(_launcher_settings())

    assert started == []
