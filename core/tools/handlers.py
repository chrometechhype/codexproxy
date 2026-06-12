from __future__ import annotations

import base64
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .executor import ToolResult, _apply_v4a_hunks, _parse_v4a_patches
from .registry import (
    APPLY_PATCH_TOOL,
    EXEC_COMMAND_TOOL,
    READ_TOOL,
    SHELL_COMMAND_TOOL,
    TEST_SYNC_TOOL,
    VIEW_IMAGE_TOOL,
    WRITE_STDIN_TOOL,
    WRITE_TOOL,
)


class _BaseHandler:
    def __init__(self, *, workspace: str | Path) -> None:
        self._workspace = Path(workspace).resolve()

    def _resolve_path(self, path: str) -> Path:
        return (self._workspace / path).resolve()

    def _safe_path(self, path: str) -> Path | None:
        resolved = self._resolve_path(path)
        try:
            resolved.relative_to(self._workspace)
            return resolved
        except ValueError:
            return None

    def _safe_write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _run_shell(self, command: str, timeout: int = 60) -> ToolResult:
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(self._workspace),
            )
            output = result.stdout or ""
            if result.stderr:
                if output:
                    output += "\n"
                output += result.stderr
            return ToolResult(
                success=result.returncode == 0,
                output=output,
                error=None
                if result.returncode == 0
                else f"exit code {result.returncode}",
                exit_code=result.returncode,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                output="",
                error=f"Command timed out after {timeout}s",
                exit_code=-1,
            )
        except Exception as exc:
            return ToolResult(success=False, output="", error=str(exc), exit_code=-1)


class ShellCommandHandler(_BaseHandler):
    """Handles ``shell_command`` tool calls with smart file-write detection."""

    def __init__(self, *, workspace: str | Path, shell_timeout: int = 60) -> None:
        super().__init__(workspace=workspace)
        self._shell_timeout = shell_timeout

    def name(self) -> str:
        return "shell_command"

    def spec(self) -> dict[str, Any]:
        return dict(SHELL_COMMAND_TOOL)

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        command = arguments.get("command", "")
        timeout_ms = arguments.get("timeout_ms", 10000)

        # Try smart file write first.
        file_result = _handle_file_write(command, self._workspace)
        if file_result is not None:
            return file_result

        timeout = max(1, min(timeout_ms / 1000, self._shell_timeout))
        return self._run_shell(command, timeout=int(timeout))


class ExecCommandHandler(_BaseHandler):
    """Handles ``exec_command`` tool calls with smart file-write detection."""

    def __init__(self, *, workspace: str | Path, shell_timeout: int = 60) -> None:
        super().__init__(workspace=workspace)
        self._shell_timeout = shell_timeout

    def name(self) -> str:
        return "exec_command"

    def spec(self) -> dict[str, Any]:
        return dict(EXEC_COMMAND_TOOL)

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        cmd = arguments.get("cmd", "")
        if isinstance(cmd, list):
            cmd = " ".join(str(c) for c in cmd)

        # Try smart file write first.
        file_result = _handle_file_write(cmd, self._workspace)
        if file_result is not None:
            return file_result

        yield_time_ms = arguments.get("yield_time_ms", 10000)
        timeout = max(1, min(yield_time_ms / 1000, self._shell_timeout))
        return self._run_shell(cmd, timeout=int(timeout))


class WriteStdinHandler(_BaseHandler):
    """Handles ``write_stdin`` tool calls (minimal stub)."""

    def __init__(self, *, workspace: str | Path, shell_timeout: int = 60) -> None:
        super().__init__(workspace=workspace)
        self._shell_timeout = shell_timeout

    def name(self) -> str:
        return "write_stdin"

    def spec(self) -> dict[str, Any]:
        return dict(WRITE_STDIN_TOOL)

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(
            success=False,
            output="",
            error="write_stdin is not supported in native mode (requires running process)",
            exit_code=-1,
        )


class ApplyPatchHandler(_BaseHandler):
    """Handles ``apply_patch`` tool calls."""

    def name(self) -> str:
        return "apply_patch"

    def spec(self) -> dict[str, Any]:
        return dict(APPLY_PATCH_TOOL)

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        cmd = arguments.get("cmd", [])
        if not isinstance(cmd, list) or len(cmd) < 2:
            return ToolResult(
                success=False,
                output="",
                error="apply_patch requires cmd array with patch content",
                exit_code=-1,
            )
        patch_content = cmd[1]
        return self._apply_patch(patch_content)

    def _apply_patch(self, patch_content: str) -> ToolResult:
        patches = _parse_v4a_patches(patch_content)
        if not patches:
            return ToolResult(
                success=False,
                output="",
                error="No valid V4A patches found in patch content",
                exit_code=-1,
            )

        results: list[str] = []
        for patch in patches:
            filepath = self._safe_path(patch["file"])
            if filepath is None:
                results.append(f"SKIP {patch['file']}: outside workspace")
                continue

            if patch["mode"] == "delete":
                if filepath.exists():
                    filepath.unlink()
                    results.append(f"DELETED {patch['file']}")
                else:
                    results.append(f"SKIP {patch['file']}: not found")
            elif patch["mode"] == "create":
                content = _apply_v4a_hunks("", patch.get("hunks", []))
                self._safe_write(filepath, content)
                results.append(f"CREATED {patch['file']} ({len(content)} bytes)")
            else:
                original = (
                    filepath.read_text(encoding="utf-8") if filepath.exists() else ""
                )
                new_content = _apply_v4a_hunks(original, patch.get("hunks", []))
                if new_content == original:
                    results.append(f"UNCHANGED {patch['file']}")
                else:
                    self._safe_write(filepath, new_content)
                    results.append(
                        f"PATCHED {patch['file']} ({len(new_content)} bytes)"
                    )

        return ToolResult(success=True, output="\n".join(results))


class ReadHandler(_BaseHandler):
    """Handles ``read`` tool calls."""

    def name(self) -> str:
        return "read"

    def spec(self) -> dict[str, Any]:
        return dict(READ_TOOL)

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        path = arguments.get("path", "")
        resolved = self._safe_path(path)
        if resolved is None:
            return ToolResult(
                success=False,
                output="",
                error=f"Path '{path}' is outside workspace",
                exit_code=-1,
            )
        if not resolved.is_file():
            return ToolResult(
                success=False,
                output="",
                error=f"File not found: {resolved}",
                exit_code=-1,
            )
        try:
            content = resolved.read_text(encoding="utf-8")
            return ToolResult(success=True, output=content)
        except Exception as exc:
            return ToolResult(success=False, output="", error=str(exc), exit_code=-1)


class WriteHandler(_BaseHandler):
    """Handles ``write`` tool calls — writes file content directly."""

    def name(self) -> str:
        return "write"

    def spec(self) -> dict[str, Any]:
        return dict(WRITE_TOOL)

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        path = arguments.get("path", "")
        content = arguments.get("content", "")
        resolved = self._safe_path(path)
        if resolved is None:
            return ToolResult(
                success=False,
                output="",
                error=f"Path '{path}' is outside workspace",
                exit_code=-1,
            )
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding="utf-8")
            return ToolResult(
                success=True, output=f"Written {len(content)} bytes to {path}"
            )
        except Exception as exc:
            return ToolResult(success=False, output="", error=str(exc), exit_code=-1)


class ViewImageHandler(_BaseHandler):
    """Handles ``view_image`` tool calls."""

    def name(self) -> str:
        return "view_image"

    def spec(self) -> dict[str, Any]:
        return dict(VIEW_IMAGE_TOOL)

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        path = arguments.get("path", "")
        resolved = self._safe_path(path)
        if resolved is None or not resolved.is_file():
            return ToolResult(
                success=False,
                output="",
                error=f"Image not found: {path}",
                exit_code=-1,
            )
        try:
            data = resolved.read_bytes()
            b64 = base64.b64encode(data).decode("ascii")
            return ToolResult(
                success=True,
                output=json.dumps(
                    {
                        "image_url": f"data:image/{resolved.suffix[1:]};base64,{b64}",
                        "detail": "high",
                    }
                ),
            )
        except Exception as exc:
            return ToolResult(success=False, output="", error=str(exc), exit_code=-1)


class TestSyncHandler(_BaseHandler):
    """Handles ``test_sync_tool`` tool calls (stub for integration tests)."""

    def name(self) -> str:
        return "test_sync_tool"

    def spec(self) -> dict[str, Any]:
        return dict(TEST_SYNC_TOOL)

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(success=True, output="ok")


# ---------------------------------------------------------------------------
# Smart file-write detection for shell commands
# ---------------------------------------------------------------------------

_HEREDOC_PATTERN = re.compile(
    r"""cat\s+<<['"]?(\w+)['"]?\s*(?:[>]{1,2})\s*(\S+)\s*\n(.*?)\n\1""",
    re.DOTALL | re.MULTILINE,
)

_ECHO_WRITE_PATTERN = re.compile(
    r"""echo\s+['"](.+?)['"]\s*(?:>>|>)\s*(\S+)""",
    re.DOTALL,
)


def _handle_file_write(command: str, workspace: Path) -> ToolResult | None:
    """Detect file writes in a shell command and handle them directly.

    Recognised patterns:

    - ``cat <<'EOF' > path  (heredoc)``
    - ``cat <<EOF > path   (heredoc)``
    - ``echo 'content' > path``
    """
    # Try heredoc: cat <<'MARK' > path ... MARK
    match = _HEREDOC_PATTERN.search(command)
    if match:
        path_str = match.group(2)
        content = match.group(3)
        resolved = (workspace / path_str).resolve()
        try:
            resolved.relative_to(workspace)
        except ValueError:
            return ToolResult(
                success=False,
                output="",
                error=f"Path '{path_str}' is outside workspace",
                exit_code=-1,
            )
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return ToolResult(
            success=True,
            output=f"Written {len(content)} bytes to {path_str}",
            exit_code=0,
        )

    # Try echo > file (single line).
    match = _ECHO_WRITE_PATTERN.search(command)
    if match:
        content = match.group(1)
        path_str = match.group(2)
        mode = "w" if ">" in command and ">>" not in command else "a"
        resolved = (workspace / path_str).resolve()
        try:
            resolved.relative_to(workspace)
        except ValueError:
            return ToolResult(
                success=False,
                output="",
                error=f"Path '{path_str}' is outside workspace",
                exit_code=-1,
            )
        resolved.parent.mkdir(parents=True, exist_ok=True)
        if mode == "a" and resolved.exists():
            existing = resolved.read_text(encoding="utf-8")
            content = (
                existing
                + ("\n" if existing and not existing.endswith("\n") else "")
                + content
            )
        resolved.write_text(content, encoding="utf-8")
        return ToolResult(
            success=True,
            output=f"Written {len(content)} bytes to {path_str}",
            exit_code=0,
        )

    return None
