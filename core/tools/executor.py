from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger


@dataclass
class ToolResult:
    success: bool
    output: str
    error: str | None = None
    exit_code: int = 0


class ToolExecutor:
    """Execute coding tools locally with sandboxing."""

    def __init__(
        self,
        *,
        workspace: str | Path,
        enabled: bool = False,
        allowed_commands: tuple[str, ...] = (),
        allowed_paths: tuple[str | Path, ...] = (),
        sandbox_mode: str = "none",
        shell_timeout: int = 60,
    ) -> None:
        self._workspace = Path(workspace).resolve()
        self._enabled = enabled
        self._allowed_commands = tuple(cmd.lower() for cmd in allowed_commands)
        self._allowed_paths: tuple[Path, ...] = tuple(
            Path(p).resolve() for p in allowed_paths
        )
        self._sandbox_mode = sandbox_mode
        self._shell_timeout = shell_timeout

    def execute(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        if not self._enabled:
            return ToolResult(
                success=False,
                output="",
                error="Local tool execution is disabled. Set ENABLE_LOCAL_TOOL_EXECUTION=true to enable.",
                exit_code=-1,
            )

        handlers = {
            "apply_patch": self._execute_apply_patch,
            "shell": self._execute_shell,
            "read": self._execute_read,
            "write": self._execute_write,
            "run_tests": self._execute_run_tests,
        }
        handler = handlers.get(tool_name)
        if handler is None:
            return ToolResult(
                success=False,
                output="",
                error=f"Unknown tool: {tool_name}",
                exit_code=-1,
            )

        logger.info("Executing tool: tool={}", tool_name)
        start = time.monotonic()
        result = handler(arguments)
        elapsed = time.monotonic() - start
        logger.info(
            "Tool result: tool={} success={} elapsed={:.2f}s output_len={}",
            tool_name,
            result.success,
            elapsed,
            len(result.output),
        )
        return result

    def _execute_apply_patch(self, arguments: dict[str, Any]) -> ToolResult:
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
            filepath = self._resolve_sandbox_path(patch["file"])
            if filepath is None:
                results.append(f"SKIP {patch['file']}: outside allowed paths")
                continue

            if patch["mode"] == "delete":
                if filepath.exists():
                    self._safe_delete(filepath)
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

    def _execute_shell(self, arguments: dict[str, Any]) -> ToolResult:
        command = arguments.get("command", "") or arguments.get("cmd", "")
        if isinstance(command, list):
            command = " ".join(str(c) for c in command)
        if not command.strip():
            return ToolResult(
                success=False, output="", error="shell requires a command", exit_code=-1
            )

        cmd_name = command.strip().split()[0].lower() if command.strip() else ""
        if self._sandbox_mode != "none" and (
            not self._allowed_commands or cmd_name not in self._allowed_commands
        ):
            return ToolResult(
                success=False,
                output="",
                error=f"Command '{cmd_name}' is not in the allowed commands list ({', '.join(self._allowed_commands)})",
                exit_code=-1,
            )

        return self._run_shell(command)

    def _execute_read(self, arguments: dict[str, Any]) -> ToolResult:
        path = arguments.get("path", "") or arguments.get("file", "")
        resolved = self._resolve_sandbox_path(path)
        if resolved is None:
            return ToolResult(
                success=False,
                output="",
                error=f"Path '{path}' is outside allowed paths",
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

    def _execute_write(self, arguments: dict[str, Any]) -> ToolResult:
        path = arguments.get("path", "") or arguments.get("file", "")
        content = arguments.get("content", "")
        resolved = self._resolve_sandbox_path(path)
        if resolved is None:
            return ToolResult(
                success=False,
                output="",
                error=f"Path '{path}' is outside allowed paths",
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

    def _execute_run_tests(self, arguments: dict[str, Any]) -> ToolResult:
        command = arguments.get("command", "")
        if isinstance(command, list):
            command = " ".join(str(c) for c in command)
        if not command.strip():
            return ToolResult(
                success=False,
                output="",
                error="run_tests requires a command",
                exit_code=-1,
            )

        cmd_name = command.strip().split()[0].lower() if command.strip() else ""
        if self._sandbox_mode != "none" and (
            not self._allowed_commands or cmd_name not in self._allowed_commands
        ):
            return ToolResult(
                success=False,
                output="",
                error=f"Command '{cmd_name}' is not in the allowed commands list ({', '.join(self._allowed_commands)})",
                exit_code=-1,
            )

        return self._run_shell(command)

    def _run_shell(self, command: str) -> ToolResult:
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self._shell_timeout,
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
                error=f"Command timed out after {self._shell_timeout}s",
                exit_code=-1,
            )
        except Exception as exc:
            return ToolResult(success=False, output="", error=str(exc), exit_code=-1)

    def _resolve_sandbox_path(self, path: str) -> Path | None:
        resolved = (self._workspace / path).resolve()
        try:
            resolved.relative_to(self._workspace)
            return resolved
        except ValueError:
            if self._allowed_paths:
                for allowed in self._allowed_paths:
                    try:
                        resolved.relative_to(allowed)
                        return resolved
                    except ValueError:
                        continue
            return None

    def _safe_delete(self, path: Path) -> None:
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            import shutil

            shutil.rmtree(path)

    def _safe_write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# V4A Patch Parser
# ---------------------------------------------------------------------------

_V4A_BEGIN_RE = re.compile(r"^\*\*\*\s*Begin\s+Patch\s*\*\*\*")
_V4A_END_RE = re.compile(r"^\*\*\*\s*End\s+Patch\s*\*\*\*")
_HUNK_HEADER_RE = re.compile(r"^@@\s+-(\d+),?(\d*)\s+\+(\d+),?(\d*)\s+@@")
_UNIFIED_PATH_RE = re.compile(r"^[-+]{3}\s+(?:[ab]/)?(.+)")


def _parse_v4a_patches(content: str) -> list[dict[str, Any]]:
    """Parse V4A patch format into structured patch operations."""
    patches: list[dict[str, Any]] = []
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if _V4A_BEGIN_RE.match(line):
            result = _parse_one_patch(lines, i + 1)
            if result[0] is not None:
                patches.append(result[0])
            i = result[1] if result[1] is not None else len(lines)
        else:
            i += 1
    return patches


def _parse_one_patch(
    lines: list[str], start: int
) -> tuple[dict[str, Any] | None, int | None]:
    """Parse a single patch from *start* until ``*** End Patch ***``."""
    old_file = ""
    new_file = ""
    i = start
    while i < len(lines):
        line = lines[i]
        if _V4A_END_RE.match(line):
            i += 1
            break
        match = _UNIFIED_PATH_RE.match(line)
        if match:
            prefix = line[0]
            if prefix == "-":
                old_file = match.group(1)
            elif prefix == "+":
                new_file = match.group(1)
        i += 1
    else:
        return None, None

    _old_is_devnull = old_file == "/dev/null"
    _new_is_devnull = new_file == "/dev/null"
    _has_old = bool(old_file) and not _old_is_devnull
    _has_new = bool(new_file) and not _new_is_devnull

    patch: dict[str, Any]
    if not _has_old and _has_new:
        patch = {"file": new_file, "mode": "create"}
    elif _has_old and not _has_new:
        patch = {"file": old_file, "mode": "delete"}
    else:
        patch = {"file": new_file or old_file, "mode": "modify"}

    patch_lines = lines[start : i - 1] if i > start else []
    hunks = _parse_v4a_hunks(patch_lines)
    if hunks:
        patch["hunks"] = hunks
    else:
        patch["hunks"] = []

    return patch, i


def _parse_v4a_hunks(lines: list[str]) -> list[dict[str, Any]]:
    """Parse unified diff hunks from diff lines."""
    hunks: list[dict[str, Any]] = []
    current_hunk: dict[str, Any] | None = None
    for line in lines:
        match = _HUNK_HEADER_RE.match(line)
        if match:
            current_hunk = {
                "old_start": int(match.group(1)),
                "old_count": int(match.group(2)) if match.group(2) else 1,
                "new_start": int(match.group(3)),
                "new_count": int(match.group(4)) if match.group(4) else 1,
                "lines": [],
            }
            hunks.append(current_hunk)
        elif current_hunk is not None:
            current_hunk["lines"].append(line)
    return hunks


def _apply_v4a_hunks(original: str, hunks: list[dict[str, Any]]) -> str:
    """Apply V4A hunks to original content and return the result."""
    if not hunks:
        return original

    original_lines = original.splitlines(keepends=True)
    result = list(original_lines)

    for hunk in reversed(hunks):
        old_start = hunk["old_start"]
        old_count = max(hunk.get("old_count", 1), 0)
        new_lines = _build_v4a_result_lines(hunk["lines"])

        idx = old_start - 1
        if old_count > 0 and idx <= len(result):
            result[idx : idx + old_count] = new_lines
        elif old_count == 0 and idx <= len(result):
            result[idx:idx] = new_lines
        else:
            result.extend(new_lines)

    return "".join(result)


def _build_v4a_result_lines(hunk_lines: list[str]) -> list[str]:
    """Build result lines from a hunk, keeping additions and context."""
    return [
        line[1:] + "\n"
        for line in hunk_lines
        if line.startswith("+") or line.startswith(" ")
    ]
