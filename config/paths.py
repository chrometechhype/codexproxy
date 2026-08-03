"""Shared filesystem paths for CodexProxy configuration."""

from pathlib import Path

CODEX_PROXY_CONFIG_DIRNAME = ".codexproxy"
CODEX_PROXY_ENV_FILENAME = ".env"
LEGACY_REPO_DIRNAME = "codexproxy"
LEGACY_XDG_CONFIG_DIRNAME = ".config"
MESSAGING_STATE_DIRNAME = "agent_workspace"
CODEX_PROXY_LOGS_DIRNAME = "logs"
SERVER_LOG_FILENAME = "server.log"
CODEX_MODEL_CATALOG_FILENAME = "codex-model-catalog.json"
AUTH_DIRNAME = "auth"
OPENAI_AUTH_FILENAME = "openai.json"
OPENAI_AUTH_LOCK_FILENAME = "openai.lock"


def config_dir_path() -> Path:
    """Return the default user config directory."""

    return Path.home() / CODEX_PROXY_CONFIG_DIRNAME


def managed_env_path() -> Path:
    """Return the default user-managed env file path."""

    return config_dir_path() / CODEX_PROXY_ENV_FILENAME


def legacy_env_paths() -> tuple[Path, ...]:
    """Return legacy user env paths that can be migrated to ~/.codexproxy/.env."""

    home = Path.home()
    return (
        home / LEGACY_REPO_DIRNAME / CODEX_PROXY_ENV_FILENAME,
        home
        / LEGACY_XDG_CONFIG_DIRNAME
        / LEGACY_REPO_DIRNAME
        / CODEX_PROXY_ENV_FILENAME,
    )


def messaging_state_dir_path() -> Path:
    """Return the managed messaging state directory."""

    return config_dir_path() / MESSAGING_STATE_DIRNAME


def server_log_path() -> Path:
    """Return the canonical server log path."""

    return config_dir_path() / CODEX_PROXY_LOGS_DIRNAME / SERVER_LOG_FILENAME


def codex_model_catalog_path() -> Path:
    """Return the generated Codex model catalog path."""

    return config_dir_path() / CODEX_MODEL_CATALOG_FILENAME


def openai_auth_path() -> Path:
    """Return CodexProxy's private ChatGPT credential file path."""

    return config_dir_path() / AUTH_DIRNAME / OPENAI_AUTH_FILENAME


def openai_auth_lock_path() -> Path:
    """Return the cross-process lock path for ChatGPT credentials."""

    return config_dir_path() / AUTH_DIRNAME / OPENAI_AUTH_LOCK_FILENAME
