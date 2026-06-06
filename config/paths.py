"""Shared filesystem paths for CodexProxy configuration."""

from pathlib import Path

CODEXPROXY_CONFIG_DIRNAME = ".codexproxy"
CODEXPROXY_ENV_FILENAME = ".env"
LEGACY_PREDECESSOR_DIRNAME = "free-claude-code"
LEGACY_FCC_DIRNAME = ".fcc"
LEGACY_XDG_CONFIG_DIRNAME = ".config"
CODEX_WORKSPACE_DIRNAME = "agent_workspace"
CODEXPROXY_LOGS_DIRNAME = "logs"
SERVER_LOG_FILENAME = "server.log"


def config_dir_path() -> Path:
    """Return the default user config directory."""

    return Path.home() / CODEXPROXY_CONFIG_DIRNAME


def managed_env_path() -> Path:
    """Return the default user-managed env file path."""

    return config_dir_path() / CODEXPROXY_ENV_FILENAME


def legacy_env_paths() -> tuple[Path, ...]:
    """Return legacy user env paths that can be migrated to ~/.codexproxy/.env.

    Accepts the predecessor project's locations for migration.
    """

    home = Path.home()
    return (
        home / LEGACY_PREDECESSOR_DIRNAME / CODEXPROXY_ENV_FILENAME,
        home / LEGACY_FCC_DIRNAME / CODEXPROXY_ENV_FILENAME,
        home
        / LEGACY_XDG_CONFIG_DIRNAME
        / LEGACY_PREDECESSOR_DIRNAME
        / CODEXPROXY_ENV_FILENAME,
        home / LEGACY_XDG_CONFIG_DIRNAME / LEGACY_FCC_DIRNAME / CODEXPROXY_ENV_FILENAME,
    )


def default_codex_workspace_path() -> Path:
    """Return the default Codex workspace path."""

    return config_dir_path() / CODEX_WORKSPACE_DIRNAME


def server_log_path() -> Path:
    """Return the canonical server log path."""

    return config_dir_path() / CODEXPROXY_LOGS_DIRNAME / SERVER_LOG_FILENAME
