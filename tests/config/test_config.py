"""Tests for config/settings.py and config/nim.py"""

from typing import Any, cast

import pytest
from pydantic import ValidationError

from config.constants import (
    ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
    HTTP_CONNECT_TIMEOUT_DEFAULT,
)
from config.nim import NimSettings


class TestSettings:
    """Test Settings configuration."""

    def test_settings_loads(self):
        """Ensure Settings can be instantiated."""
        from config.settings import Settings

        settings = Settings()
        assert settings is not None

    def test_default_values(self, monkeypatch):
        """Test default values are set and have correct types."""
        from config.settings import Settings

        monkeypatch.delenv("CLAUDE_WORKSPACE", raising=False)
        monkeypatch.delenv("MODEL", raising=False)
        monkeypatch.delenv("HTTP_READ_TIMEOUT", raising=False)
        monkeypatch.delenv("HTTP_CONNECT_TIMEOUT", raising=False)
        monkeypatch.setitem(Settings.model_config, "env_file", ())
        settings = Settings()
        assert settings.model == "nvidia_nim/nvidia/nemotron-3-super-120b-a12b"
        assert isinstance(settings.provider_rate_limit, int)
        assert isinstance(settings.provider_rate_window, int)
        assert isinstance(settings.nim.temperature, float)
        assert isinstance(settings.fast_prefix_detection, bool)
        assert isinstance(settings.enable_model_thinking, bool)
        assert settings.http_read_timeout == 300.0
        assert settings.http_connect_timeout == HTTP_CONNECT_TIMEOUT_DEFAULT
        assert settings.log_raw_api_payloads is False
        assert settings.log_raw_sse_events is False
        assert settings.debug_platform_edits is False
        assert settings.debug_subagent_stack is False

    def test_default_codex_workspace_uses_codexproxy_home(self, monkeypatch, tmp_path):
        """Unset workspace stores agent data under ~/.codexproxy."""
        from config.settings import Settings

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setitem(Settings.model_config, "env_file", ())

        settings = Settings()

        assert settings.codex_workspace == str(
            tmp_path / ".codexproxy" / "agent_workspace"
        )
        # Deprecated alias still resolves to the new path.
        assert settings.claude_workspace == settings.codex_workspace

    def test_server_log_path_uses_codexproxy_home(self, monkeypatch, tmp_path):
        """The server log location is fixed under ~/.codexproxy."""
        from config.paths import server_log_path

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

        assert server_log_path() == tmp_path / ".codexproxy" / "logs" / "server.log"

    def test_removed_log_file_env_is_ignored(self, monkeypatch):
        """Legacy LOG_FILE values do not affect Settings or block startup."""
        from config.settings import Settings

        monkeypatch.setenv("LOG_FILE", "custom/server.log")
        monkeypatch.setitem(Settings.model_config, "env_file", ())

        settings = Settings()

        assert not hasattr(settings, "log_file")

    def test_stale_zai_base_url_env_is_ignored(self, monkeypatch):
        """Cloud Z.ai endpoint is fixed in provider metadata, not settings."""
        from config.settings import Settings

        monkeypatch.setenv("ZAI_BASE_URL", "https://custom.zai.invalid/v1")
        monkeypatch.setitem(Settings.model_config, "env_file", ())

        settings = Settings()

        assert not hasattr(settings, "zai_base_url")

    def test_codex_proxy_auth_token_env_loads(self, monkeypatch):
        """The new Codex auth token env var loads into codex_proxy_auth_token."""
        from config.settings import Settings

        monkeypatch.setenv("CODEX_PROXY_AUTH_TOKEN", "cdx-token")
        monkeypatch.setitem(Settings.model_config, "env_file", ())

        settings = Settings()

        assert settings.codex_proxy_auth_token == "cdx-token"
        assert settings.effective_auth_token == "cdx-token"

    def test_legacy_anthropic_auth_token_is_used_as_fallback(
        self, monkeypatch, tmp_path
    ):
        """If only ANTHROPIC_AUTH_TOKEN is set, effective_auth_token uses it."""
        from config.settings import Settings

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.delenv("CODEX_PROXY_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "legacy-token")
        monkeypatch.setitem(Settings.model_config, "env_file", ())

        settings = Settings()

        assert settings.codex_proxy_auth_token == ""
        assert settings.anthropic_auth_token == "legacy-token"
        assert settings.effective_auth_token == "legacy-token"

    def test_codex_proxy_auth_token_takes_precedence(self, monkeypatch):
        """When both are set, the new Codex token wins."""
        from config.settings import Settings

        monkeypatch.setenv("CODEX_PROXY_AUTH_TOKEN", "cdx-token")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "legacy-token")
        monkeypatch.setitem(Settings.model_config, "env_file", ())

        settings = Settings()

        assert settings.effective_auth_token == "cdx-token"

    def test_codex_cli_bin_is_codex(self, monkeypatch):
        """The default CLI binary name is ``codex``."""
        from config.settings import Settings

        monkeypatch.setitem(Settings.model_config, "env_file", ())

        settings = Settings()

        assert settings.codex_cli_bin == "codex"
        # Deprecated alias resolves to the new binary.
        assert settings.claude_cli_bin == "codex"

    def test_get_settings_cached(self):
        """Test get_settings returns cached instance."""
        from config.settings import get_settings

        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2  # Same object (cached)

    def test_empty_string_to_none_for_optional_int(self):
        """Test that empty string converts to None for optional int fields."""
        from config.settings import Settings

        # Settings should handle NVIDIA_NIM_SEED="" gracefully
        settings = Settings()
        assert settings.nim.seed is None or isinstance(settings.nim.seed, int)

    def test_model_setting(self):
        """Test model setting exists and is a string."""
        from config.settings import Settings

        settings = Settings()
        assert isinstance(settings.model, str)
        assert len(settings.model) > 0

    def test_base_url_constant(self):
        """Test NVIDIA_NIM_DEFAULT_BASE is a constant."""
        from providers.nvidia_nim import NVIDIA_NIM_DEFAULT_BASE

        assert NVIDIA_NIM_DEFAULT_BASE == "https://integrate.api.nvidia.com/v1"

    def test_lm_studio_base_url_from_env(self, monkeypatch):
        """LM_STUDIO_BASE_URL env var is loaded into settings."""
        from config.settings import Settings

        monkeypatch.setenv("LM_STUDIO_BASE_URL", "http://custom:5678/v1")
        settings = Settings()
        assert settings.lm_studio_base_url == "http://custom:5678/v1"

    def test_ollama_base_url_defaults_to_root(self, monkeypatch):
        """OLLAMA_BASE_URL defaults to the Anthropic-compatible Ollama root URL."""
        from config.settings import Settings

        monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
        monkeypatch.setitem(Settings.model_config, "env_file", ())
        settings = Settings()
        assert settings.ollama_base_url == "http://localhost:11434"

    def test_ollama_base_url_rejects_v1_suffix(self, monkeypatch):
        """OLLAMA_BASE_URL must not include /v1 for native Anthropic messages."""
        from config.settings import Settings

        monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        with pytest.raises(ValidationError, match="without /v1"):
            Settings()

    def test_provider_rate_limit_from_env(self, monkeypatch):
        """PROVIDER_RATE_LIMIT env var is loaded into settings."""
        from config.settings import Settings

        monkeypatch.setenv("PROVIDER_RATE_LIMIT", "20")
        settings = Settings()
        assert settings.provider_rate_limit == 20

    def test_provider_rate_window_from_env(self, monkeypatch):
        """PROVIDER_RATE_WINDOW env var is loaded into settings."""
        from config.settings import Settings

        monkeypatch.setenv("PROVIDER_RATE_WINDOW", "30")
        settings = Settings()
        assert settings.provider_rate_window == 30

    def test_http_read_timeout_from_env(self, monkeypatch):
        """HTTP_READ_TIMEOUT env var is loaded into settings."""
        from config.settings import Settings

        monkeypatch.setenv("HTTP_READ_TIMEOUT", "600")
        settings = Settings()
        assert settings.http_read_timeout == 600.0

    def test_http_write_timeout_from_env(self, monkeypatch):
        """HTTP_WRITE_TIMEOUT env var is loaded into settings."""
        from config.settings import Settings

        monkeypatch.setenv("HTTP_WRITE_TIMEOUT", "20")
        settings = Settings()
        assert settings.http_write_timeout == 20.0

    def test_http_connect_timeout_from_env(self, monkeypatch):
        """HTTP_CONNECT_TIMEOUT env var is loaded into settings."""
        from config.settings import Settings

        monkeypatch.setenv("HTTP_CONNECT_TIMEOUT", "5")
        settings = Settings()
        assert settings.http_connect_timeout == 5.0

    def test_http_connect_timeout_default_matches_shared_constant(
        self, monkeypatch
    ) -> None:
        """Default must match config.constants (and README / .env.example)."""
        from config.settings import Settings

        monkeypatch.delenv("HTTP_CONNECT_TIMEOUT", raising=False)
        monkeypatch.setitem(Settings.model_config, "env_file", ())
        settings = Settings()
        assert settings.http_connect_timeout == HTTP_CONNECT_TIMEOUT_DEFAULT
        assert HTTP_CONNECT_TIMEOUT_DEFAULT == 10.0

    def test_enable_model_thinking_from_env(self, monkeypatch):
        """ENABLE_MODEL_THINKING env var is loaded into settings."""
        from config.settings import Settings

        monkeypatch.setenv("ENABLE_MODEL_THINKING", "false")
        settings = Settings()
        assert settings.enable_model_thinking is False

    def test_wafer_api_key_from_env(self, monkeypatch):
        """WAFER_API_KEY env var is loaded into settings."""
        from config.settings import Settings

        monkeypatch.setenv("WAFER_API_KEY", "wafer-key")
        settings = Settings()
        assert settings.wafer_api_key == "wafer-key"

    def test_anthropic_auth_token_from_env_without_dotenv_key(self, monkeypatch):
        """ANTHROPIC_AUTH_TOKEN env var is loaded when dotenv does not define it."""
        from config.settings import Settings

        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "process-token")
        monkeypatch.setitem(Settings.model_config, "env_file", ())
        settings = Settings()
        assert settings.anthropic_auth_token == "process-token"
        assert settings.uses_process_anthropic_auth_token() is True

    def test_empty_dotenv_anthropic_auth_token_overrides_process_env(
        self, monkeypatch, tmp_path
    ):
        """An explicit empty .env token disables auth despite stale shell tokens."""
        from config.settings import Settings

        env_file = tmp_path / ".env"
        env_file.write_text("ANTHROPIC_AUTH_TOKEN=\n", encoding="utf-8")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "stale-client-token")
        monkeypatch.setitem(Settings.model_config, "env_file", (env_file,))

        settings = Settings()
        assert settings.anthropic_auth_token == ""
        assert settings.uses_process_anthropic_auth_token() is False

    def test_dotenv_anthropic_auth_token_overrides_process_env(
        self, monkeypatch, tmp_path
    ):
        """A configured .env token is the server token even with a stale shell token."""
        from config.settings import Settings

        env_file = tmp_path / ".env"
        env_file.write_text(
            'ANTHROPIC_AUTH_TOKEN="server-token"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "stale-client-token")
        monkeypatch.setitem(Settings.model_config, "env_file", (env_file,))

        settings = Settings()
        assert settings.anthropic_auth_token == "server-token"
        assert settings.uses_process_anthropic_auth_token() is False

    @pytest.mark.parametrize("removed_key", ["NIM_ENABLE_THINKING", "ENABLE_THINKING"])
    def test_removed_thinking_env_keys_are_ignored(self, monkeypatch, removed_key):
        """Stale thinking env keys do not block startup or affect settings."""
        from config.settings import Settings

        monkeypatch.setenv(removed_key, "false")
        monkeypatch.setitem(Settings.model_config, "env_file", ())

        settings = Settings()

        assert settings.enable_model_thinking is True

    @pytest.mark.parametrize("removed_key", ["NIM_ENABLE_THINKING", "ENABLE_THINKING"])
    @pytest.mark.parametrize("value", ["false", ""])
    def test_removed_thinking_dotenv_keys_are_ignored(
        self, monkeypatch, tmp_path, removed_key, value
    ):
        """Stale thinking dotenv keys do not block startup or affect settings."""
        from config.settings import Settings

        env_file = tmp_path / ".env"
        env_file.write_text(f"{removed_key}={value}\n", encoding="utf-8")
        monkeypatch.delenv(removed_key, raising=False)
        monkeypatch.setitem(Settings.model_config, "env_file", (env_file,))

        settings = Settings()

        assert settings.enable_model_thinking is True


# --- NimSettings Validation Tests ---
class TestNimSettingsValidBounds:
    """Test that valid values within bounds are accepted."""

    @pytest.mark.parametrize("top_k", [-1, 0, 1, 100])
    def test_top_k_valid(self, top_k):
        """top_k >= -1 should be accepted."""
        s = NimSettings(top_k=top_k)
        assert s.top_k == top_k

    @pytest.mark.parametrize("temp", [0.0, 0.5, 1.0, 2.0])
    def test_temperature_valid(self, temp):
        s = NimSettings(temperature=temp)
        assert s.temperature == temp

    @pytest.mark.parametrize("top_p", [0.0, 0.5, 1.0])
    def test_top_p_valid(self, top_p):
        s = NimSettings(top_p=top_p)
        assert s.top_p == top_p

    def test_max_tokens_valid(self):
        s = NimSettings(max_tokens=1)
        assert s.max_tokens == 1

    def test_min_tokens_valid(self):
        s = NimSettings(min_tokens=0)
        assert s.min_tokens == 0

    @pytest.mark.parametrize("penalty", [-2.0, 0.0, 2.0])
    def test_presence_penalty_valid(self, penalty):
        s = NimSettings(presence_penalty=penalty)
        assert s.presence_penalty == penalty

    @pytest.mark.parametrize("penalty", [-2.0, 0.0, 2.0])
    def test_frequency_penalty_valid(self, penalty):
        s = NimSettings(frequency_penalty=penalty)
        assert s.frequency_penalty == penalty

    @pytest.mark.parametrize("min_p", [0.0, 0.5, 1.0])
    def test_min_p_valid(self, min_p):
        s = NimSettings(min_p=min_p)
        assert s.min_p == min_p


class TestNimSettingsInvalidBounds:
    """Test that out-of-range values raise ValidationError."""

    @pytest.mark.parametrize("top_k", [-2, -100])
    def test_top_k_below_lower_bound(self, top_k):
        with pytest.raises((ValidationError, ValueError)):
            NimSettings(top_k=top_k)

    def test_temperature_negative(self):
        with pytest.raises(ValidationError):
            NimSettings(temperature=-0.1)

    @pytest.mark.parametrize("top_p", [-0.1, 1.1])
    def test_top_p_out_of_range(self, top_p):
        with pytest.raises(ValidationError):
            NimSettings(top_p=top_p)

    @pytest.mark.parametrize("penalty", [-2.1, 2.1])
    def test_presence_penalty_out_of_range(self, penalty):
        with pytest.raises(ValidationError):
            NimSettings(presence_penalty=penalty)

    @pytest.mark.parametrize("penalty", [-2.1, 2.1])
    def test_frequency_penalty_out_of_range(self, penalty):
        with pytest.raises(ValidationError):
            NimSettings(frequency_penalty=penalty)

    @pytest.mark.parametrize("min_p", [-0.1, 1.1])
    def test_min_p_out_of_range(self, min_p):
        with pytest.raises(ValidationError):
            NimSettings(min_p=min_p)

    @pytest.mark.parametrize("max_tokens", [0, -1])
    def test_max_tokens_too_low(self, max_tokens):
        with pytest.raises(ValidationError):
            NimSettings(max_tokens=max_tokens)

    def test_min_tokens_negative(self):
        with pytest.raises(ValidationError):
            NimSettings(min_tokens=-1)


class TestNimSettingsValidators:
    """Test custom field validators in NimSettings."""

    def test_default_max_tokens_matches_shared_constant(self):
        assert NimSettings().max_tokens == ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS

    @pytest.mark.parametrize(
        "seed_val,expected",
        [("", None), (None, None), ("42", 42), (42, 42)],
        ids=["empty_str", "none", "str_42", "int_42"],
    )
    def test_parse_optional_int(self, seed_val, expected):
        s = NimSettings(seed=seed_val)
        assert s.seed == expected

    @pytest.mark.parametrize(
        "stop_val,expected",
        [("", None), ("STOP", "STOP"), (None, None)],
        ids=["empty_str", "valid", "none"],
    )
    def test_parse_optional_str_stop(self, stop_val, expected):
        s = NimSettings(stop=stop_val)
        assert s.stop == expected

    @pytest.mark.parametrize(
        "chat_template_val,expected",
        [("", None), ("template", "template")],
        ids=["empty_str", "valid"],
    )
    def test_parse_optional_str_chat_template(self, chat_template_val, expected):
        s = NimSettings(chat_template=chat_template_val)
        assert s.chat_template == expected

    def test_extra_forbid_rejects_unknown_field(self):
        """NimSettings with extra='forbid' rejects unknown fields."""

        with pytest.raises(ValidationError):
            NimSettings(**cast(Any, {"unknown_field": "value"}))

    def test_enable_thinking_field_removed(self):
        """NimSettings no longer accepts the removed thinking toggle."""

        with pytest.raises(ValidationError):
            NimSettings(**cast(Any, {"enable_thinking": True}))


class TestSettingsOptionalStr:
    """Test Settings parse_optional_str validator."""

    def test_empty_telegram_token_to_none(self, monkeypatch):
        from config.settings import Settings

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
        s = Settings()
        assert s.telegram_bot_token is None

    def test_valid_telegram_token_preserved(self, monkeypatch):
        from config.settings import Settings

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc123")
        s = Settings()
        assert s.telegram_bot_token == "abc123"

    def test_empty_allowed_user_id_to_none(self, monkeypatch):
        from config.settings import Settings

        monkeypatch.setenv("ALLOWED_TELEGRAM_USER_ID", "")
        s = Settings()
        assert s.allowed_telegram_user_id is None

    def test_discord_bot_token_from_env(self, monkeypatch):
        from config.settings import Settings

        monkeypatch.setenv("DISCORD_BOT_TOKEN", "discord_token_123")
        s = Settings()
        assert s.discord_bot_token == "discord_token_123"

    def test_empty_discord_bot_token_to_none(self, monkeypatch):
        from config.settings import Settings

        monkeypatch.setenv("DISCORD_BOT_TOKEN", "")
        s = Settings()
        assert s.discord_bot_token is None

    def test_allowed_discord_channels_from_env(self, monkeypatch):
        from config.settings import Settings

        monkeypatch.setenv("ALLOWED_DISCORD_CHANNELS", "111,222,333")
        s = Settings()
        assert s.allowed_discord_channels == "111,222,333"

    def test_messaging_platform_from_env(self, monkeypatch):
        from config.settings import Settings

        monkeypatch.setenv("MESSAGING_PLATFORM", "discord")
        s = Settings()
        assert s.messaging_platform == "discord"

    def test_whisper_device_auto_rejected(self, monkeypatch):
        """WHISPER_DEVICE=auto raises ValidationError (auto removed)."""
        from config.settings import Settings

        monkeypatch.setenv("WHISPER_DEVICE", "auto")
        with pytest.raises(ValidationError, match="whisper_device"):
            Settings()

    @pytest.mark.parametrize("device", ["cpu", "cuda"])
    def test_whisper_device_valid(self, monkeypatch, device):
        """Valid whisper_device values are accepted."""
        from config.settings import Settings

        monkeypatch.setenv("WHISPER_DEVICE", device)
        s = Settings()
        assert s.whisper_device == device


class TestPerModelMapping:
    """Test per-model fields and resolve_model()."""

    def test_model_default_from_env(self, monkeypatch):
        """MODEL env var overrides the built-in default model."""
        from config.settings import Settings

        monkeypatch.setenv("MODEL", "open_router/deepseek/deepseek-r1")
        s = Settings()
        assert s.model == "open_router/deepseek/deepseek-r1"

    @pytest.mark.parametrize(
        "env_var,model_string",
        [
            ("MODEL", "nvidia_nim/meta/llama3-70b-instruct"),
            ("MODEL", "deepseek/deepseek-chat"),
            ("MODEL", "wafer/DeepSeek-V4-Pro"),
            ("MODEL", "lmstudio/qwen2.5-7b"),
            ("MODEL", "llamacpp/local-model"),
            ("MODEL", "ollama/llama3.1"),
        ],
    )
    def test_settings_model_from_env(self, monkeypatch, env_var, model_string):
        """MODEL env var is loaded for every supported provider."""
        from config.settings import Settings

        monkeypatch.setenv(env_var, model_string)
        s = Settings()
        assert s.model == model_string

    def test_model_invalid_provider_raises(self, monkeypatch):
        """MODEL with invalid provider prefix raises ValidationError."""
        from config.settings import Settings

        monkeypatch.setenv("MODEL", "bad_provider/some-model")
        with pytest.raises(ValidationError, match="Invalid provider"):
            Settings()

    def test_model_no_slash_raises(self, monkeypatch):
        """MODEL without provider prefix raises ValidationError."""
        from config.settings import Settings

        monkeypatch.setenv("MODEL", "noprefix")
        with pytest.raises(ValidationError, match="provider type"):
            Settings()

    def test_resolve_model_always_returns_default(self):
        """resolve_model returns the configured default MODEL for every name."""
        from config.settings import Settings

        s = Settings()
        s.model = "nvidia_nim/fallback-model"
        assert s.resolve_model("claude-opus-4-20250514") == "nvidia_nim/fallback-model"
        assert (
            s.resolve_model("claude-sonnet-4-20250514") == "nvidia_nim/fallback-model"
        )
        assert s.resolve_model("claude-3-haiku-20240307") == "nvidia_nim/fallback-model"
        assert s.resolve_model("claude-2.1") == "nvidia_nim/fallback-model"
        assert s.resolve_model("some-unknown-model") == "nvidia_nim/fallback-model"

    def test_parse_provider_type(self):
        """parse_provider_type extracts provider from model string."""
        from config.settings import Settings

        assert Settings.parse_provider_type("nvidia_nim/meta/llama") == "nvidia_nim"
        assert Settings.parse_provider_type("open_router/deepseek/r1") == "open_router"
        assert (
            Settings.parse_provider_type("mistral/devstral-small-latest") == "mistral"
        )
        assert (
            Settings.parse_provider_type("mistral_codestral/codestral-latest")
            == "mistral_codestral"
        )
        assert Settings.parse_provider_type("deepseek/deepseek-chat") == "deepseek"
        assert Settings.parse_provider_type("lmstudio/qwen") == "lmstudio"
        assert Settings.parse_provider_type("llamacpp/model") == "llamacpp"
        assert Settings.parse_provider_type("ollama/llama3.1") == "ollama"
        assert Settings.parse_provider_type("wafer/DeepSeek-V4-Pro") == "wafer"
        assert (
            Settings.parse_provider_type("gemini/models/gemini-3.1-flash-lite")
            == "gemini"
        )
        assert Settings.parse_provider_type("groq/llama-3.3-70b-versatile") == "groq"
        assert Settings.parse_provider_type("cerebras/llama3.1-8b") == "cerebras"

    def test_parse_model_name(self):
        """parse_model_name extracts model name from model string."""
        from config.settings import Settings

        assert Settings.parse_model_name("nvidia_nim/meta/llama") == "meta/llama"
        assert (
            Settings.parse_model_name("mistral/devstral-small-latest")
            == "devstral-small-latest"
        )
        assert (
            Settings.parse_model_name("mistral_codestral/codestral-latest")
            == "codestral-latest"
        )
        assert Settings.parse_model_name("deepseek/deepseek-chat") == "deepseek-chat"
        assert Settings.parse_model_name("lmstudio/qwen") == "qwen"
        assert Settings.parse_model_name("llamacpp/model") == "model"
        assert Settings.parse_model_name("ollama/llama3.1") == "llama3.1"
        assert Settings.parse_model_name("wafer/DeepSeek-V4-Pro") == "DeepSeek-V4-Pro"
        assert (
            Settings.parse_model_name("gemini/models/gemini-3.1-flash-lite")
            == "models/gemini-3.1-flash-lite"
        )
        assert (
            Settings.parse_model_name("groq/llama-3.3-70b-versatile")
            == "llama-3.3-70b-versatile"
        )
        assert Settings.parse_model_name("cerebras/llama3.1-8b") == "llama3.1-8b"

    def test_configured_chat_model_refs_collects_unique_models_with_sources(
        self, monkeypatch
    ):
        """Startup validation model collection is limited to the configured MODEL."""
        from config.settings import Settings

        monkeypatch.setenv("CODEX_PROXY_SMOKE_MODEL_NVIDIA_NIM", "nvidia_nim/smoke")
        monkeypatch.setenv("WHISPER_MODEL", "openai/whisper-large-v3")
        s = Settings()
        s.model = "nvidia_nim/fallback"

        refs = s.configured_chat_model_refs()

        assert [ref.model_ref for ref in refs] == ["nvidia_nim/fallback"]
        assert refs[0].provider_id == "nvidia_nim"
        assert refs[0].model_id == "fallback"
        assert refs[0].sources == ("MODEL",)
