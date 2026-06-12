from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from loguru import logger

from .executor import ToolResult


class ToolHandler(Protocol):
    """Protocol for tool handlers."""

    def name(self) -> str: ...

    def spec(self) -> dict[str, Any]: ...

    async def execute(self, arguments: dict[str, Any]) -> ToolResult: ...


class ToolRegistry:
    """Registry of tool handlers.

    Each handler is registered with a tool name, its JSON schema
    definition, and an async execute function.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, handler: ToolHandler) -> None:
        self._handlers[handler.name()] = handler

    def get_specs(self) -> list[dict[str, Any]]:
        return [h.spec() for h in self._handlers.values()]

    def get_handler(self, name: str) -> ToolHandler | None:
        return self._handlers.get(name)

    def has_tool(self, name: str) -> bool:
        return name in self._handlers

    async def dispatch(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        handler = self._handlers.get(name)
        if handler is None:
            return ToolResult(
                success=False,
                output="",
                error=f"Unknown tool: {name}",
                exit_code=-1,
            )
        logger.debug("Dispatching tool: name={}", name)
        return await handler.execute(arguments)


# ---------------------------------------------------------------------------
# Default tool definitions (sent to non-OpenAI providers as function tools)
# ---------------------------------------------------------------------------

SHELL_COMMAND_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "shell_command",
    "description": (
        "Run a shell command in the user's default shell and return its "
        "output. Always set the `workdir` parameter - do not use `cd`."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell script to execute.",
            },
            "workdir": {
                "type": "string",
                "description": "Working directory. Defaults to the workspace root.",
            },
            "timeout_ms": {
                "type": "number",
                "description": "Maximum runtime in ms. Defaults to 10000.",
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    },
}

EXEC_COMMAND_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "exec_command",
    "description": (
        "Run a command in a PTY, returning output or a session ID "
        "for ongoing interaction."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "cmd": {
                "type": "string",
                "description": "Shell command to execute.",
            },
            "workdir": {
                "type": "string",
                "description": "Working directory. Defaults to the turn cwd.",
            },
            "tty": {
                "type": "boolean",
                "description": "True allocates a PTY; false or omitted uses plain pipes.",
            },
            "yield_time_ms": {
                "type": "number",
                "description": "Wait before yielding output. Defaults to 10000 ms.",
            },
            "max_output_tokens": {
                "type": "number",
                "description": "Output token budget. Defaults to 10000 tokens.",
            },
        },
        "required": ["cmd"],
        "additionalProperties": False,
    },
}

WRITE_STDIN_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "write_stdin",
    "description": "Write characters to an existing running process session.",
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "number",
                "description": "Identifier of the running exec session.",
            },
            "chars": {
                "type": "string",
                "description": "Bytes to write to stdin. Empty polls without writing.",
            },
            "yield_time_ms": {
                "type": "number",
                "description": "Wait before yielding output.",
            },
        },
        "required": ["session_id"],
        "additionalProperties": False,
    },
}

APPLY_PATCH_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "apply_patch",
    "description": (
        "Create, update, edit, or delete files using a standard unified diff format. "
        "YOU MUST USE THIS TOOL FOR ALL FILE EDITING — never use shell commands for file operations.\\n\\n"
        "Format:\\n"
        "*** Begin Patch ***\\n"
        "--- a/file.py\\n"
        "+++ b/file.py\\n"
        "@@ -1,3 +1,4 @@\\n"
        " ...context lines...\\n"
        "+new line\\n"
        "-removed line\\n"
        "*** End Patch ***\\n\\n"
        "To create a new file: use /dev/null as old file.\\n"
        "To delete a file: use /dev/null as new file."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "patch": {
                "type": "string",
                "description": (
                    "The full patch content between *** Begin Patch *** and *** End Patch *** markers.\\n\\n"
                    "Examples:\\n\\n"
                    "CREATE a new file:\\n"
                    "*** Begin Patch ***\\n"
                    "--- /dev/null\\n"
                    "+++ b/main.py\\n"
                    "@@ -0,0 +1,3 @@\\n"
                    "+#!/usr/bin/env python3\\n"
                    "+print('hello')\\n"
                    "*** End Patch ***\\n\\n"
                    "EDIT an existing file:\\n"
                    "*** Begin Patch ***\\n"
                    "--- a/main.py\\n"
                    "+++ b/main.py\\n"
                    "@@ -1,3 +1,4 @@\\n"
                    " print('hello')\\n"
                    "+print('world')\\n"
                    "*** End Patch ***\\n\\n"
                    "DELETE a file:\\n"
                    "*** Begin Patch ***\\n"
                    "--- a/main.py\\n"
                    "+++ /dev/null\\n"
                    "@@ -1,3 +0,0 @@\\n"
                    "-#!/usr/bin/env python3\\n"
                    "-print('hello')\\n"
                    "*** End Patch ***"
                ),
            },
        },
        "required": ["patch"],
        "additionalProperties": False,
    },
}

VIEW_IMAGE_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "view_image",
    "description": "View a local image file from the filesystem.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Local filesystem path to an image file.",
            },
            "detail": {
                "type": "string",
                "enum": ["high", "original"],
                "description": "Image detail level. Defaults to `high`.",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    },
}

READ_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "read",
    "description": "Read the contents of a file from the local filesystem.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file to read (relative to workspace).",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    },
}

WRITE_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "write",
    "description": (
        "Write content directly to a file, creating or overwriting it. "
        "Always use this for creating or writing files instead of shell commands."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path to write (relative to workspace).",
            },
            "content": {
                "type": "string",
                "description": "Full text content to write to the file.",
            },
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    },
}

TEST_SYNC_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "test_sync_tool",
    "description": "Internal test synchronization helper.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["sync_all", "sync_file", "sync_directory"],
                "description": "Sync action to perform.",
            },
            "path": {
                "type": "string",
                "description": "File or directory path for the sync action.",
            },
        },
        "additionalProperties": False,
    },
}

# The tool specs that CodexProxy advertises to non-OpenAI providers.
DEFAULT_TOOL_SPECS: list[dict[str, Any]] = [
    SHELL_COMMAND_TOOL,
    EXEC_COMMAND_TOOL,
    APPLY_PATCH_TOOL,
    READ_TOOL,
    WRITE_TOOL,
    VIEW_IMAGE_TOOL,
    TEST_SYNC_TOOL,
]


def build_default_registry(
    workspace: str | Path,
    sandbox_mode: str = "none",
    shell_timeout: int = 60,
) -> ToolRegistry:
    """Build a ToolRegistry with default handlers."""
    from .handlers import (
        ApplyPatchHandler,
        ExecCommandHandler,
        ReadHandler,
        ShellCommandHandler,
        TestSyncHandler,
        ViewImageHandler,
        WriteHandler,
        WriteStdinHandler,
    )

    registry = ToolRegistry()
    registry.register(
        ShellCommandHandler(workspace=workspace, shell_timeout=shell_timeout)
    )
    registry.register(
        ExecCommandHandler(workspace=workspace, shell_timeout=shell_timeout)
    )
    registry.register(ApplyPatchHandler(workspace=workspace))
    registry.register(ReadHandler(workspace=workspace))
    registry.register(WriteHandler(workspace=workspace))
    registry.register(ViewImageHandler(workspace=workspace))
    registry.register(
        WriteStdinHandler(workspace=workspace, shell_timeout=shell_timeout)
    )
    registry.register(TestSyncHandler(workspace=workspace))
    return registry
