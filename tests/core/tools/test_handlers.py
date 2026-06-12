from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from core.tools.handlers import (
    ReadHandler,
    ShellCommandHandler,
    WriteHandler,
    _handle_file_write,
)


class TestHandleFileWrite:
    def test_heredoc_write(self, tmp_path: Path) -> None:
        cmd = textwrap.dedent("""\
            cat <<'EOF' > hello.py
            #!/usr/bin/env python3
            print("hello")
            EOF
        """)
        result = _handle_file_write(cmd, tmp_path)
        assert result is not None
        assert result.success
        target = tmp_path / "hello.py"
        assert target.is_file()
        content = target.read_text(encoding="utf-8")
        assert 'print("hello")' in content

    def test_heredoc_append(self, tmp_path: Path) -> None:
        existing = tmp_path / "log.txt"
        existing.write_text("before\n", encoding="utf-8")
        cmd = textwrap.dedent("""\
            cat <<'EOF' >> log.txt
            after
            EOF
        """)
        result = _handle_file_write(cmd, tmp_path)
        assert result is not None
        assert result.success
        content = existing.read_text(encoding="utf-8")
        assert "before" not in content

    def test_heredoc_without_quoted_marker(self, tmp_path: Path) -> None:
        cmd = textwrap.dedent("""\
            cat <<EOF > data.txt
            hello world
            EOF
        """)
        result = _handle_file_write(cmd, tmp_path)
        assert result is not None
        assert result.success
        target = tmp_path / "data.txt"
        assert target.is_file()
        assert target.read_text(encoding="utf-8") == "hello world"

    def test_heredoc_outside_workspace(self, tmp_path: Path) -> None:
        cmd = textwrap.dedent("""\
            cat <<'EOF' > /tmp/malicious.txt
            bad
            EOF
        """)
        result = _handle_file_write(cmd, tmp_path)
        assert result is not None
        assert not result.success
        assert "outside workspace" in (result.error or "")

    def test_echo_write(self, tmp_path: Path) -> None:
        cmd = "echo 'hello world' > greeting.txt"
        result = _handle_file_write(cmd, tmp_path)
        assert result is not None
        assert result.success
        target = tmp_path / "greeting.txt"
        assert target.is_file()
        assert target.read_text(encoding="utf-8") == "hello world"

    def test_echo_append(self, tmp_path: Path) -> None:
        existing = tmp_path / "notes.txt"
        existing.write_text("line1\n", encoding="utf-8")
        cmd = "echo 'line2' >> notes.txt"
        result = _handle_file_write(cmd, tmp_path)
        assert result is not None
        assert result.success
        content = existing.read_text(encoding="utf-8")
        assert "line1" in content
        assert "line2" in content

    def test_echo_outside_workspace(self, tmp_path: Path) -> None:
        cmd = "echo 'bad' > /tmp/evil.txt"
        result = _handle_file_write(cmd, tmp_path)
        assert result is not None
        assert not result.success
        assert "outside workspace" in (result.error or "")

    def test_regular_shell_command_not_affected(self, tmp_path: Path) -> None:
        cmd = "uv --version"
        result = _handle_file_write(cmd, tmp_path)
        assert result is None

    def test_cd_command_not_affected(self, tmp_path: Path) -> None:
        cmd = "cd /tmp && ls"
        result = _handle_file_write(cmd, tmp_path)
        assert result is None


class TestShellCommandHandler:
    @pytest.mark.asyncio
    async def test_handles_heredoc_write(self, tmp_path: Path) -> None:
        handler = ShellCommandHandler(workspace=tmp_path)
        cmd = textwrap.dedent("""\
            cat <<'EOF' > out.txt
            content
            EOF
        """)
        result = await handler.execute({"command": cmd})
        assert result.success
        target = tmp_path / "out.txt"
        assert target.is_file()
        assert "content" in target.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_regular_command_passes_through(self, tmp_path: Path) -> None:
        handler = ShellCommandHandler(workspace=tmp_path)
        result = await handler.execute({"command": "echo hello"})
        assert result.success
        assert "hello" in result.output

    @pytest.mark.asyncio
    async def test_echo_write_handled_natively(self, tmp_path: Path) -> None:
        handler = ShellCommandHandler(workspace=tmp_path)
        result = await handler.execute({"command": "echo 'test123' > test.txt"})
        assert result.success
        target = tmp_path / "test.txt"
        assert target.is_file()
        assert target.read_text(encoding="utf-8") == "test123"


class TestReadHandler:
    @pytest.mark.asyncio
    async def test_reads_file(self, tmp_path: Path) -> None:
        target = tmp_path / "data.txt"
        target.write_text("hello world", encoding="utf-8")
        handler = ReadHandler(workspace=tmp_path)
        result = await handler.execute({"path": "data.txt"})
        assert result.success
        assert result.output == "hello world"

    @pytest.mark.asyncio
    async def test_file_not_found(self, tmp_path: Path) -> None:
        handler = ReadHandler(workspace=tmp_path)
        result = await handler.execute({"path": "missing.txt"})
        assert not result.success
        assert "not found" in (result.error or "")

    @pytest.mark.asyncio
    async def test_outside_workspace(self, tmp_path: Path) -> None:
        handler = ReadHandler(workspace=tmp_path)
        result = await handler.execute({"path": "../outside.txt"})
        assert not result.success
        assert "outside workspace" in (result.error or "")


class TestWriteHandler:
    @pytest.mark.asyncio
    async def test_writes_new_file(self, tmp_path: Path) -> None:
        handler = WriteHandler(workspace=tmp_path)
        result = await handler.execute({"path": "new.txt", "content": "data"})
        assert result.success
        assert tmp_path.joinpath("new.txt").read_text(encoding="utf-8") == "data"

    @pytest.mark.asyncio
    async def test_creates_directories(self, tmp_path: Path) -> None:
        handler = WriteHandler(workspace=tmp_path)
        result = await handler.execute({"path": "a/b/c/nested.txt", "content": "deep"})
        assert result.success
        assert (
            tmp_path.joinpath("a/b/c/nested.txt").read_text(encoding="utf-8") == "deep"
        )

    @pytest.mark.asyncio
    async def test_outside_workspace_blocked(self, tmp_path: Path) -> None:
        handler = WriteHandler(workspace=tmp_path)
        result = await handler.execute({"path": "/tmp/outside.txt", "content": "x"})
        assert not result.success
        assert "outside workspace" in (result.error or "")
