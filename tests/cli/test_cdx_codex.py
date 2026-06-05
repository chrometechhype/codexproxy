"""Tests for cli/entrypoints.py — cdx-codex config writer helpers."""

from __future__ import annotations

import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from config.settings import Settings


def _settings(
    *, model: str = "nvidia_nim/test-model", token: str = "freecc"
) -> Settings:
    return Settings.model_construct(
        host="0.0.0.0",
        port=8082,
        anthropic_auth_token=token,
        model=model,
    )


def test_toml_quote_escapes_backslash_and_quote() -> None:
    from cli.entrypoints import _toml_quote

    assert _toml_quote('a"b') == 'a\\"b'
    assert _toml_quote("a\\b") == "a\\\\b"
    assert _toml_quote('a"b\\c') == 'a\\"b\\\\c'


def test_default_codex_model_strips_provider_prefix() -> None:
    from cli.entrypoints import _default_codex_model

    assert (
        _default_codex_model(_settings(model="nvidia_nim/test-model")) == "test-model"
    )
    assert _default_codex_model(_settings(model="bare")) == "bare"
    assert _default_codex_model(_settings(model="/leading-slash")) == "leading-slash"


def test_default_codex_model_falls_back_when_empty() -> None:
    from cli.entrypoints import _default_codex_model

    assert _default_codex_model(_settings(model="")) == "gpt-4o"


def test_write_codex_config_creates_file_with_block(tmp_path: Path) -> None:
    from cli.entrypoints import _write_codex_config

    p = tmp_path / "config.toml"
    _write_codex_config(
        p,
        base_url="http://127.0.0.1:8082/v1",
        api_key="freecc",
        model="test-model",
    )

    text = p.read_text(encoding="utf-8")
    assert ">>> codexproxy (managed by cdx-codex) >>>" in text
    assert "<<< codexproxy <<<" in text
    assert "[model_providers.codexproxy]" in text
    assert 'base_url = "http://127.0.0.1:8082/v1"' in text
    assert 'api_key = "freecc"' in text
    assert 'wire_api = "responses"' in text
    assert 'model = "test-model"' in text
    assert 'model_provider = "codexproxy"' in text


def test_write_codex_config_produces_parseable_toml(tmp_path: Path) -> None:
    from cli.entrypoints import _write_codex_config

    p = tmp_path / "config.toml"
    _write_codex_config(
        p,
        base_url="http://127.0.0.1:8082/v1",
        api_key="freecc",
        model="test-model",
    )

    parsed = tomllib.loads(p.read_text(encoding="utf-8"))
    provider = parsed["model_providers"]["codexproxy"]
    assert provider["base_url"] == "http://127.0.0.1:8082/v1"
    assert provider["api_key"] == "freecc"
    assert provider["wire_api"] == "responses"
    assert provider["requires_openai_auth"] is True
    assert provider["env"]["OPENAI_API_KEY"] == "freecc"
    assert parsed["codexproxy"]["model"] == "test-model"
    assert parsed["codexproxy"]["model_provider"] == "codexproxy"
    assert parsed["codexproxy"]["approval_policy"] == "never"


def test_write_codex_config_preserves_existing_user_settings(tmp_path: Path) -> None:
    from cli.entrypoints import _write_codex_config

    p = tmp_path / "config.toml"
    p.write_text(
        '[model_providers.other]\nname = "other"\nbase_url = "https://example.com"\n',
        encoding="utf-8",
    )

    _write_codex_config(
        p,
        base_url="http://127.0.0.1:8082/v1",
        api_key="freecc",
        model="test-model",
    )

    parsed = tomllib.loads(p.read_text(encoding="utf-8"))
    assert "other" in parsed["model_providers"]
    assert parsed["model_providers"]["other"]["base_url"] == "https://example.com"
    assert "codexproxy" in parsed["model_providers"]


def test_write_codex_config_is_idempotent_on_second_call(tmp_path: Path) -> None:
    from cli.entrypoints import _write_codex_config

    p = tmp_path / "config.toml"
    _write_codex_config(
        p,
        base_url="http://127.0.0.1:8082/v1",
        api_key="freecc",
        model="test-model",
    )
    first = p.read_text(encoding="utf-8")

    _write_codex_config(
        p,
        base_url="http://127.0.0.1:9090/v1",
        api_key="freecc",
        model="test-model",
    )
    second = p.read_text(encoding="utf-8")

    assert first.count(">>> codexproxy (managed by cdx-codex) >>>") == 1
    assert second.count(">>> codexproxy (managed by cdx-codex) >>>") == 1
    assert "http://127.0.0.1:8082/v1" in first
    assert "http://127.0.0.1:8082/v1" not in second
    assert "http://127.0.0.1:9090/v1" in second


def test_write_codex_config_creates_parent_directory(tmp_path: Path) -> None:
    from cli.entrypoints import _write_codex_config

    p = tmp_path / "nested" / "config.toml"
    assert not p.parent.exists()

    _write_codex_config(
        p,
        base_url="http://127.0.0.1:8082/v1",
        api_key="freecc",
        model="test-model",
    )

    assert p.is_file()


def test_codex_config_path_alt_uses_codex_home_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cli import entrypoints

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "custom_codex"))
    assert (
        entrypoints._codex_config_path_alt()
        == tmp_path / "custom_codex" / "config.toml"
    )


def test_codex_config_path_alt_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli import entrypoints

    monkeypatch.delenv("CODEX_HOME", raising=False)
    with patch("pathlib.Path.home") as home:
        home.return_value = Path("/tmp/fake-home")
        result = entrypoints._codex_config_path_alt()
    assert result == Path("/tmp/fake-home/.codex/config.toml")


def test_launch_codex_writes_config_and_executes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """launch_codex must write ~/.codex/config.toml and exec the codex binary."""
    from cli import entrypoints

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_PROXY_AUTH_TOKEN", "freecc")
    from config.settings import get_settings

    get_settings.cache_clear()

    fake_config_path = tmp_path / "config.toml"

    class FakeProcess:
        pid = 42424
        returncode = 0

        def wait(self) -> int:
            return self.returncode

    popen_calls: list[dict[str, object]] = []

    def fake_popen(cmd: list[str], env: object = None) -> FakeProcess:
        popen_calls.append({"cmd": list(cmd), "env": env})
        return FakeProcess()

    monkeypatch.setattr(entrypoints.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(entrypoints, "_preflight_proxy", lambda _url: None)
    monkeypatch.setattr(
        entrypoints.shutil, "which", lambda _name: "/usr/local/bin/codex"
    )

    with pytest.raises(SystemExit) as excinfo:
        entrypoints.launch_codex(["--version"])

    assert excinfo.value.code == 0
    assert fake_config_path.is_file()
    parsed = tomllib.loads(fake_config_path.read_text(encoding="utf-8"))
    assert parsed["model_providers"]["codexproxy"]["wire_api"] == "responses"
    first_call = popen_calls[0]
    first_cmd = first_call["cmd"]
    assert isinstance(first_cmd, list)
    assert first_cmd[0] == "/usr/local/bin/codex"
    assert first_cmd[1:] == ["--version"]


def test_launch_codex_errors_when_proxy_unreachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cli import entrypoints

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    from config.settings import get_settings

    get_settings.cache_clear()

    monkeypatch.setattr(
        entrypoints, "_preflight_proxy", lambda _url: "connection refused"
    )
    monkeypatch.setattr(
        entrypoints.shutil, "which", lambda _name: "/usr/local/bin/codex"
    )

    with pytest.raises(SystemExit) as excinfo:
        entrypoints.launch_codex([])
    assert excinfo.value.code == 1
    assert not (tmp_path / "config.toml").exists()


def test_launch_codex_errors_when_codex_binary_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cli import entrypoints

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    from config.settings import get_settings

    get_settings.cache_clear()

    monkeypatch.setattr(entrypoints, "_preflight_proxy", lambda _url: None)
    monkeypatch.setattr(entrypoints.shutil, "which", lambda _name: None)

    with pytest.raises(SystemExit) as excinfo:
        entrypoints.launch_codex([])
    assert excinfo.value.code == 127


def test_launch_codex_propagates_process_returncode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cli import entrypoints

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    from config.settings import get_settings

    get_settings.cache_clear()

    class FakeProcess:
        pid = 99999
        returncode = 7

        def wait(self) -> int:
            return self.returncode

    monkeypatch.setattr(entrypoints, "_preflight_proxy", lambda _url: None)
    monkeypatch.setattr(
        entrypoints.shutil, "which", lambda _name: "/usr/local/bin/codex"
    )
    monkeypatch.setattr(
        entrypoints.subprocess, "Popen", lambda *_a, **_k: FakeProcess()
    )

    with pytest.raises(SystemExit) as excinfo:
        entrypoints.launch_codex([])
    assert excinfo.value.code == 7
