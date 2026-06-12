"""Tests for the ToolExecutor."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from core.tools.executor import (
    ToolExecutor,
    _apply_v4a_hunks,
    _parse_v4a_patches,
)

# ---------------------------------------------------------------------------
# V4A patch parser
# ---------------------------------------------------------------------------


class TestParseV4aPatches:
    def test_empty_content(self) -> None:
        assert _parse_v4a_patches("") == []

    def test_no_patch_markers(self) -> None:
        assert _parse_v4a_patches("some random text\nno markers here") == []

    def test_single_create_patch(self) -> None:
        content = textwrap.dedent("""\
            *** Begin Patch ***
            --- /dev/null
            +++ b/new_file.py
            @@ -0,0 +1,3 @@
            +def hello():
            +    print("world")
            +
            *** End Patch ***
        """)
        patches = _parse_v4a_patches(content)
        assert len(patches) == 1
        assert patches[0]["file"] == "new_file.py"
        assert patches[0]["mode"] == "create"

    def test_single_modify_patch(self) -> None:
        content = textwrap.dedent("""\
            *** Begin Patch ***
            --- a/file.py
            +++ b/file.py
            @@ -1,3 +1,4 @@
             line1
            -line2
            +line2_modified
             line3
            *** End Patch ***
        """)
        patches = _parse_v4a_patches(content)
        assert len(patches) == 1
        assert patches[0]["file"] == "file.py"
        assert patches[0]["mode"] == "modify"

    def test_multiple_patches(self) -> None:
        content = textwrap.dedent("""\
            *** Begin Patch ***
            --- a/a.py
            +++ b/a.py
            @@ -1 +1,2 @@
             old
            +new
            *** End Patch ***
            *** Begin Patch ***
            --- a/b.py
            +++ b/b.py
            @@ -1 +1,2 @@
             hello
            +world
            *** End Patch ***
        """)
        patches = _parse_v4a_patches(content)
        assert len(patches) == 2

    def test_delete_patch(self) -> None:
        content = textwrap.dedent("""\
            *** Begin Patch ***
            --- a/to_delete.py
            +++ /dev/null
            @@ -1,3 +0,0 @@
            -line1
            -line2
            -line3
            *** End Patch ***
        """)
        patches = _parse_v4a_patches(content)
        assert len(patches) == 1
        assert patches[0]["mode"] == "delete"
        assert patches[0]["file"] == "to_delete.py"


def test_apply_v4a_hunks_empty() -> None:
    assert _apply_v4a_hunks("original", []) == "original"


def test_apply_v4a_hunks_append() -> None:
    original = "line1\nline2\n"
    hunks = [
        {
            "old_start": 2,
            "old_count": 1,
            "new_start": 2,
            "new_count": 2,
            "lines": [" line2", "+line3"],
        }
    ]
    result = _apply_v4a_hunks(original, hunks)
    assert result == "line1\nline2\nline3\n"


def test_apply_v4a_hunks_create_new_file() -> None:
    original = ""
    hunks = [
        {
            "old_start": 1,
            "old_count": 0,
            "new_start": 1,
            "new_count": 2,
            "lines": ["+def foo():", "+    pass"],
        }
    ]
    result = _apply_v4a_hunks(original, hunks)
    assert result == "def foo():\n    pass\n"


# ---------------------------------------------------------------------------
# ToolExecutor
# ---------------------------------------------------------------------------


@pytest.fixture
def executor(tmp_path: Path) -> ToolExecutor:
    return ToolExecutor(
        workspace=tmp_path,
        enabled=True,
        allowed_commands=("uv", "python", "pytest", "git"),
        sandbox_mode="restrictive",
    )


@pytest.fixture
def disabled_executor(tmp_path: Path) -> ToolExecutor:
    return ToolExecutor(
        workspace=tmp_path,
        enabled=False,
    )


class TestToolExecutorApplyPatch:
    def test_disabled(self, disabled_executor: ToolExecutor) -> None:
        result = disabled_executor.execute("apply_patch", {"cmd": ["apply_patch", ""]})
        assert not result.success
        assert "disabled" in (result.error or "")

    def test_missing_cmd(self, executor: ToolExecutor) -> None:
        result = executor.execute("apply_patch", {})
        assert not result.success
        assert "cmd array" in (result.error or "")

    def test_create_new_file(self, executor: ToolExecutor, tmp_path: Path) -> None:
        patch = textwrap.dedent("""\
            *** Begin Patch ***
            --- /dev/null
            +++ b/hello.py
            @@ -0,0 +1,3 @@
            +def greet():
            +    return "hello"
            +
            *** End Patch ***
        """)
        result = executor.execute("apply_patch", {"cmd": ["apply_patch", patch]})
        assert result.success
        assert "CREATED" in result.output
        created = tmp_path / "hello.py"
        assert created.is_file()
        assert "def greet()" in created.read_text(encoding="utf-8")

    def test_modify_existing_file(self, executor: ToolExecutor, tmp_path: Path) -> None:
        target = tmp_path / "file.py"
        target.write_text("old_line\n", encoding="utf-8")
        patch = textwrap.dedent("""\
            *** Begin Patch ***
            --- a/file.py
            +++ b/file.py
            @@ -1 +1,2 @@
             old_line
            +new_line
            *** End Patch ***
        """)
        result = executor.execute("apply_patch", {"cmd": ["apply_patch", patch]})
        assert result.success
        assert "PATCHED" in result.output
        content = target.read_text(encoding="utf-8")
        assert "old_line" in content
        assert "new_line" in content

    def test_delete_file(self, executor: ToolExecutor, tmp_path: Path) -> None:
        target = tmp_path / "old.py"
        target.write_text("delete me", encoding="utf-8")
        patch = textwrap.dedent("""\
            *** Begin Patch ***
            --- a/old.py
            +++ /dev/null
            @@ -1 +0,0 @@
            -delete me
            *** End Patch ***
        """)
        result = executor.execute("apply_patch", {"cmd": ["apply_patch", patch]})
        assert result.success
        assert "DELETED" in result.output
        assert not target.exists()

    def test_outside_workspace_blocked(self, executor: ToolExecutor) -> None:
        patch = textwrap.dedent("""\
            *** Begin Patch ***
            --- /dev/null
            +++ b/../../etc/passwd
            @@ -0,0 +1 @@
            +hacked
            *** End Patch ***
        """)
        result = executor.execute("apply_patch", {"cmd": ["apply_patch", patch]})
        assert result.success
        assert "SKIP" in result.output
        assert "outside allowed" in result.output


class TestToolExecutorShell:
    def test_disabled(self, disabled_executor: ToolExecutor) -> None:
        result = disabled_executor.execute("shell", {"command": "echo hi"})
        assert not result.success
        assert "disabled" in (result.error or "")

    def test_execute_allowed_command(self, executor: ToolExecutor) -> None:
        result = executor.execute("shell", {"command": "uv --version"})
        assert result.success
        assert "uv" in result.output

    def test_disallowed_command(self, executor: ToolExecutor) -> None:
        result = executor.execute("shell", {"command": "rm -rf /"})
        assert not result.success
        assert "not in the allowed commands" in (result.error or "")

    def test_missing_command(self, executor: ToolExecutor) -> None:
        result = executor.execute("shell", {})
        assert not result.success

    def test_exit_code_error(self, executor: ToolExecutor) -> None:
        result = executor.execute("shell", {"command": 'python -c "exit(1)"'})
        assert not result.success
        assert "exit code" in (result.error or "")


class TestToolExecutorRead:
    def test_read_file(self, executor: ToolExecutor, tmp_path: Path) -> None:
        target = tmp_path / "data.txt"
        target.write_text("hello world", encoding="utf-8")
        result = executor.execute("read", {"path": "data.txt", "file": ""})
        assert result.success
        assert result.output == "hello world"

    def test_read_file_with_file_key(
        self, executor: ToolExecutor, tmp_path: Path
    ) -> None:
        target = tmp_path / "data.txt"
        target.write_text("content", encoding="utf-8")
        result = executor.execute("read", {"file": "data.txt"})
        assert result.success
        assert result.output == "content"

    def test_read_nonexistent(self, executor: ToolExecutor) -> None:
        result = executor.execute("read", {"path": "nonexistent.txt"})
        assert not result.success
        assert "not found" in (result.error or "")

    def test_read_outside_workspace(self, executor: ToolExecutor) -> None:
        result = executor.execute("read", {"path": "../outside.txt"})
        assert not result.success
        assert "outside allowed" in (result.error or "")


class TestToolExecutorWrite:
    def test_write_new_file(self, executor: ToolExecutor, tmp_path: Path) -> None:
        result = executor.execute("write", {"path": "new_file.txt", "content": "hello"})
        assert result.success
        assert tmp_path.joinpath("new_file.txt").read_text(encoding="utf-8") == "hello"

    def test_write_outside_workspace(self, executor: ToolExecutor) -> None:
        result = executor.execute("write", {"path": "../outside.txt", "content": "x"})
        assert not result.success

    def test_write_with_nested_path(
        self, executor: ToolExecutor, tmp_path: Path
    ) -> None:
        result = executor.execute(
            "write", {"path": "sub/dir/file.txt", "content": "nested"}
        )
        assert result.success
        assert (
            tmp_path.joinpath("sub/dir/file.txt").read_text(encoding="utf-8")
            == "nested"
        )


class TestToolExecutorRunTests:
    def test_run_tests_command(self, executor: ToolExecutor) -> None:
        result = executor.execute("run_tests", {"command": "uv --version"})
        assert result.success
        assert "uv" in result.output

    def test_run_tests_missing_command(self, executor: ToolExecutor) -> None:
        result = executor.execute("run_tests", {})
        assert not result.success
        assert "requires a command" in (result.error or "")

    def test_run_tests_disallowed(self, executor: ToolExecutor) -> None:
        result = executor.execute("run_tests", {"command": "rm -rf"})
        assert not result.success


class TestToolExecutorUnknown:
    def test_unknown_tool(self, executor: ToolExecutor) -> None:
        result = executor.execute("unknown_tool", {})
        assert not result.success
        assert "Unknown tool" in (result.error or "")


class TestAllAllowedCommands:
    def test_empty_allowed_blocks_all(self, tmp_path: Path) -> None:
        ex = ToolExecutor(
            workspace=tmp_path,
            enabled=True,
            allowed_commands=(),
            sandbox_mode="restrictive",
        )
        result = ex.execute("shell", {"command": "uv --version"})
        assert not result.success
        assert "not in the allowed commands" in (result.error or "")
