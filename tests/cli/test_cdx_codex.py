"""Tests for cli/entrypoints.py — cdx-codex config writer helpers."""

from __future__ import annotations

import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from config.settings import Settings


def _settings(
    *, model: str = "nvidia_nim/test-model", token: str = "codexproxy"
) -> Settings:
    return Settings.model_construct(
        host="0.0.0.0",
        port=8083,
        anthropic_auth_token=token,
        model=model,
    )


def test_toml_quote_escapes_backslash_and_quote() -> None:
    from cli.entrypoints import _toml_quote

    assert _toml_quote('a"b') == 'a\\"b'
    assert _toml_quote("a\\b") == "a\\\\b"
    assert _toml_quote('a"b\\c') == 'a\\"b\\\\c'


def test_update_top_level_settings_replaces_existing_model() -> None:
    from cli.entrypoints import _update_top_level_codex_settings

    text = (
        'model = "proxy-model"\n'
        'model_provider = "codexproxy"\n'
        'model_reasoning_effort = "xhigh"\n'
        "\n"
        'sandbox_mode = "danger-full-access"\n'
        "\n"
        "[model_providers.codexproxy]\n"
        'base_url = "http://localhost:8083/v1/"\n'
    )
    updated = _update_top_level_codex_settings(text, model="test-model")

    parsed = tomllib.loads(updated)
    assert parsed["model"] == "test-model"
    assert parsed["model_provider"] == "codexproxy"
    assert parsed["model_reasoning_effort"] == "xhigh"
    assert parsed["sandbox_mode"] == "danger-full-access"
    assert parsed["model_providers"]["codexproxy"]["base_url"] == (
        "http://localhost:8083/v1/"
    )


def test_update_top_level_settings_preserves_user_sections() -> None:
    from cli.entrypoints import _update_top_level_codex_settings

    text = (
        'model = "old-model"\n'
        "\n"
        "[plugins.browser]\n"
        "enabled = true\n"
        "\n"
        "[mcp_servers.node_repl]\n"
        "args = []\n"
    )
    updated = _update_top_level_codex_settings(
        text, model="new-model", provider="codexproxy"
    )

    parsed = tomllib.loads(updated)
    assert parsed["model"] == "new-model"
    assert parsed["model_provider"] == "codexproxy"
    assert parsed["plugins"]["browser"]["enabled"] is True
    assert parsed["mcp_servers"]["node_repl"]["args"] == []


def test_update_top_level_settings_inserts_missing_keys() -> None:
    from cli.entrypoints import _update_top_level_codex_settings

    text = 'sandbox_mode = "danger-full-access"\n'
    updated = _update_top_level_codex_settings(text, model="test-model")

    parsed = tomllib.loads(updated)
    assert parsed["model"] == "test-model"
    assert parsed["model_provider"] == "codexproxy"
    assert parsed["sandbox_mode"] == "danger-full-access"


def test_update_top_level_settings_quotes_special_chars_in_model() -> None:
    from cli.entrypoints import _update_top_level_codex_settings

    text = 'model = "old"\n'
    updated = _update_top_level_codex_settings(text, model='evil"model\\name')

    assert 'model = "evil\\"model\\\\name"' in updated
    parsed = tomllib.loads(updated)
    assert parsed["model"] == 'evil"model\\name'

    updated2 = _update_top_level_codex_settings(text, model="has\\backslash")
    assert 'model = "has\\\\backslash"' in updated2
    parsed2 = tomllib.loads(updated2)
    assert parsed2["model"] == "has\\backslash"


def test_update_top_level_settings_does_not_match_nested_keys() -> None:
    from cli.entrypoints import _update_top_level_codex_settings

    text = (
        'model = "old-model"\n'
        "\n"
        "[features]\n"
        "multi_agent = true\n"
        "\n"
        "[model_providers.codexproxy]\n"
        'model = "nested-model"\n'
        'name = "codexproxy"\n'
    )
    updated = _update_top_level_codex_settings(text, model="new-model")

    parsed = tomllib.loads(updated)
    assert parsed["model"] == "new-model"
    assert parsed["model_providers"]["codexproxy"]["model"] == "nested-model"


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
        base_url="http://127.0.0.1:8083/v1",
        api_key="codexproxy",
        model="test-model",
    )

    text = p.read_text(encoding="utf-8")
    assert ">>> codexproxy (managed by cdx-codex) >>>" in text
    assert "<<< codexproxy <<<" in text
    assert "[model_providers.codexproxy]" in text
    assert 'base_url = "http://127.0.0.1:8083/v1"' in text
    assert 'api_key = "codexproxy"' in text
    assert 'wire_api = "responses"' in text
    assert 'model = "test-model"' in text
    assert 'model_provider = "codexproxy"' in text


def test_write_codex_config_produces_parseable_toml(tmp_path: Path) -> None:
    from cli.entrypoints import _write_codex_config

    p = tmp_path / "config.toml"
    _write_codex_config(
        p,
        base_url="http://127.0.0.1:8083/v1",
        api_key="codexproxy",
        model="test-model",
    )

    parsed = tomllib.loads(p.read_text(encoding="utf-8"))
    provider = parsed["model_providers"]["codexproxy"]
    assert provider["base_url"] == "http://127.0.0.1:8083/v1"
    assert provider["api_key"] == "codexproxy"
    assert provider["wire_api"] == "responses"
    assert "requires_openai_auth" not in provider
    assert provider["env"]["OPENAI_API_KEY"] == "codexproxy"
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
        base_url="http://127.0.0.1:8083/v1",
        api_key="codexproxy",
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
        base_url="http://127.0.0.1:8083/v1",
        api_key="codexproxy",
        model="test-model",
    )
    first = p.read_text(encoding="utf-8")

    _write_codex_config(
        p,
        base_url="http://127.0.0.1:8083/v1",
        api_key="codexproxy",
        model="test-model",
    )
    second = p.read_text(encoding="utf-8")

    assert first.count(">>> codexproxy (managed by cdx-codex) >>>") == 1
    assert second.count(">>> codexproxy (managed by cdx-codex) >>>") == 1
    assert "http://127.0.0.1:8083/v1" in first
    assert "http://127.0.0.1:8083/v1" in second
    assert first == second


def test_write_codex_config_creates_parent_directory(tmp_path: Path) -> None:
    from cli.entrypoints import _write_codex_config

    p = tmp_path / "nested" / "config.toml"
    assert not p.parent.exists()

    _write_codex_config(
        p,
        base_url="http://127.0.0.1:8083/v1",
        api_key="codexproxy",
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
    monkeypatch.setenv("CODEX_PROXY_AUTH_TOKEN", "codexproxy")
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


def test_write_codex_config_updates_existing_top_level_model(
    tmp_path: Path,
) -> None:
    """When the user already has ``model = \"...\"`` we must rewrite it so the
    Codex Desktop app picks a model the proxy actually advertises."""
    from cli.entrypoints import _write_codex_config

    p = tmp_path / "config.toml"
    p.write_text(
        'model = "proxy-model"\n'
        'model_provider = "codexproxy"\n'
        'model_reasoning_effort = "xhigh"\n'
        "\n"
        "[model_providers.ollama-launch]\n"
        'name = "Ollama"\n'
        'base_url = "http://127.0.0.1:11434/v1/"\n',
        encoding="utf-8",
    )

    _write_codex_config(
        p,
        base_url="http://127.0.0.1:19095/v1",
        api_key="codexproxy",
        model="test-model",
    )

    parsed = tomllib.loads(p.read_text(encoding="utf-8"))
    assert parsed["model"] == "test-model"
    assert parsed["model_provider"] == "codexproxy"
    assert parsed["model_reasoning_effort"] == "xhigh"
    assert parsed["model_providers"]["ollama-launch"]["base_url"] == (
        "http://127.0.0.1:11434/v1/"
    )
    assert parsed["model_providers"]["codexproxy"]["base_url"] == (
        "http://127.0.0.1:19095/v1"
    )
    assert parsed["model_providers"]["codexproxy"]["api_key"] == "codexproxy"


def test_configure_codex_writes_config_and_exits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli import entrypoints

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_PROXY_AUTH_TOKEN", "codexproxy")
    from config.settings import get_settings

    get_settings.cache_clear()

    monkeypatch.setattr(entrypoints, "_preflight_proxy", lambda _url: None)

    entrypoints.configure_codex()

    config_path = tmp_path / "config.toml"
    assert config_path.is_file()
    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["model_providers"]["codexproxy"]["wire_api"] == "responses"
    assert parsed["model"] == parsed["codexproxy"]["model"]

    out = capsys.readouterr().out
    assert "Codex CLI config written to" in out
    assert "base_url" in out
    assert "api_key" in out
    assert "Codex Desktop app" in out


def test_configure_codex_errors_when_proxy_unreachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cli import entrypoints

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    from config.settings import get_settings

    get_settings.cache_clear()

    monkeypatch.setattr(
        entrypoints, "_preflight_proxy", lambda _url: "connection refused"
    )

    with pytest.raises(SystemExit) as excinfo:
        entrypoints.configure_codex()
    assert excinfo.value.code == 1
    assert not (tmp_path / "config.toml").exists()


def test_configure_codex_updates_existing_top_level_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``cdx-codex-config`` must replace the user's top-level ``model`` so the
    Codex Desktop app picks a real model on its next refresh."""
    from cli import entrypoints

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_PROXY_AUTH_TOKEN", "codexproxy")
    from config.settings import get_settings

    get_settings.cache_clear()

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'model = "proxy-model"\n'
        'model_provider = "codexproxy"\n'
        "\n"
        "[plugins.browser]\n"
        "enabled = true\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(entrypoints, "_preflight_proxy", lambda _url: None)
    entrypoints.configure_codex()

    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["model"] == parsed["codexproxy"]["model"]
    assert parsed["model"] != "proxy-model"
    assert parsed["model_provider"] == "codexproxy"
    assert parsed["plugins"]["browser"]["enabled"] is True


# ---------------------------------------------------------------------------
# Backup / restore defaults
# ---------------------------------------------------------------------------


def test_write_codex_config_creates_backup_on_first_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli import entrypoints

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'model = "user-model"\n'
        'model_provider = "openai"\n'
        "\n"
        "[plugins]\n"
        "browser = { enabled = true }\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(entrypoints, "_codex_config_path_alt", lambda: config_path)
    monkeypatch.setattr(entrypoints, "_preflight_proxy", lambda _url: None)

    entrypoints.configure_codex()

    backup = tmp_path / "config.toml.codexproxy-backup"
    assert backup.is_file()
    assert 'model = "user-model"' in backup.read_text(encoding="utf-8")
    assert "[plugins]" in backup.read_text(encoding="utf-8")


def test_write_codex_config_does_not_overwrite_existing_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli import entrypoints

    config_path = tmp_path / "config.toml"
    backup = tmp_path / "config.toml.codexproxy-backup"
    backup.write_text('model = "old-original"\n', encoding="utf-8")
    config_path.write_text(
        'model = "current"\n'
        "\n"
        "# >>> codexproxy (managed by cdx-codex) >>>\n"
        "[model_providers.codexproxy]\n"
        'name = "codexproxy"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(entrypoints, "_codex_config_path_alt", lambda: config_path)
    monkeypatch.setattr(entrypoints, "_preflight_proxy", lambda _url: None)

    entrypoints.configure_codex()

    assert backup.read_text(encoding="utf-8") == 'model = "old-original"\n'


def test_restore_codex_defaults_restores_config_from_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli import entrypoints

    config_path = tmp_path / "config.toml"
    backup = tmp_path / "config.toml.codexproxy-backup"
    config_path.write_text(
        'model = "codexproxy-model"\n'
        'model_provider = "codexproxy"\n'
        "\n"
        "# >>> codexproxy (managed by cdx-codex) >>>\n"
        "[model_providers.codexproxy]\n",
        encoding="utf-8",
    )
    backup.write_text('model = "user-original-model"\n', encoding="utf-8")
    monkeypatch.setattr(entrypoints, "_codex_config_path_alt", lambda: config_path)
    monkeypatch.setattr(entrypoints, "_clear_user_env_var", lambda _name: True)

    result = entrypoints.restore_codex_defaults()

    assert "restored" in result
    assert any("config:" in r for r in result["restored"])
    assert config_path.read_text(encoding="utf-8") == 'model = "user-original-model"\n'
    assert "OPENAI_BASE_URL" in result["cleared_env"]
    assert "OPENAI_API_KEY" in result["cleared_env"]


def test_restore_codex_defaults_falls_back_to_legacy_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli import entrypoints

    config_path = tmp_path / "config.toml"
    legacy = tmp_path / "config.toml.backup_pre_cdx"
    config_path.write_text('model = "codexproxy-model"\n', encoding="utf-8")
    legacy.write_text('model = "legacy-original"\n', encoding="utf-8")
    monkeypatch.setattr(entrypoints, "_codex_config_path_alt", lambda: config_path)
    monkeypatch.setattr(entrypoints, "_clear_user_env_var", lambda _name: True)

    result = entrypoints.restore_codex_defaults()

    assert any("backup_pre_cdx" in r for r in result["restored"])
    assert config_path.read_text(encoding="utf-8") == 'model = "legacy-original"\n'


def test_restore_codex_defaults_reports_missing_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli import entrypoints

    config_path = tmp_path / "config.toml"
    config_path.write_text('model = "codexproxy-model"\n', encoding="utf-8")
    monkeypatch.setattr(entrypoints, "_codex_config_path_alt", lambda: config_path)
    monkeypatch.setattr(entrypoints, "_clear_user_env_var", lambda _name: True)

    result = entrypoints.restore_codex_defaults()

    assert any("no backup" in s for s in result["skipped"])
    assert config_path.read_text(encoding="utf-8") == 'model = "codexproxy-model"\n'
