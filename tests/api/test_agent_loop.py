"""Tests for the agent loop and tool execution integration."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.models.responses import (
    ResponsesCreateRequest,
)
from api.responses_service import (
    _build_continuation_request,
    _merge_usage,
    _parse_failover_models,
)
from config.settings import get_settings

app = create_app()


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    """Clear the cached settings before each test to avoid stale values."""
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Helper tests
# ---------------------------------------------------------------------------


class TestParseFailoverModels:
    def test_empty(self) -> None:
        assert _parse_failover_models("") == []

    def test_single(self) -> None:
        assert _parse_failover_models("open_router/gpt-4o") == ["open_router/gpt-4o"]

    def test_multiple(self) -> None:
        result = _parse_failover_models("open_router/gpt-4o, gemini/gemini-2.0-flash")
        assert result == ["open_router/gpt-4o", "gemini/gemini-2.0-flash"]

    def test_with_whitespace(self) -> None:
        assert _parse_failover_models("  a/b , c/d  ") == ["a/b", "c/d"]


class TestMergeUsage:
    def test_empty(self) -> None:
        target: dict[str, Any] = {}
        _merge_usage(target, {})
        assert target == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    def test_merge_once(self) -> None:
        target: dict[str, Any] = {}
        _merge_usage(
            target, {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}
        )
        assert target["input_tokens"] == 10
        assert target["output_tokens"] == 20
        assert target["total_tokens"] == 30

    def test_merge_twice(self) -> None:
        target: dict[str, Any] = {}
        _merge_usage(
            target, {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}
        )
        _merge_usage(
            target, {"input_tokens": 5, "output_tokens": 10, "total_tokens": 15}
        )
        assert target["input_tokens"] == 15
        assert target["output_tokens"] == 30
        assert target["total_tokens"] == 45


class TestBuildContinuationRequest:
    def test_adds_tool_call_and_output(self) -> None:
        request = ResponsesCreateRequest(
            model="gpt-4o",
            input=[{"type": "message", "role": "user", "content": "hello"}],
        )
        tool_calls = [
            {
                "call_id": "fc_1",
                "name": "apply_patch",
                "arguments": '{"cmd":["apply_patch","..."]}',
            }
        ]
        tool_results = [{"call_id": "fc_1", "output": "patched!", "success": True}]
        new = _build_continuation_request(request, tool_calls, tool_results)

        input_items = new.input if isinstance(new.input, list) else []
        types = [
            getattr(item, "type", None)
            if not isinstance(item, dict)
            else item.get("type")
            for item in input_items
        ]
        assert "function_call" in types
        assert "function_call_output" in types


# ---------------------------------------------------------------------------
# Agent loop integration tests (via TestClient + stub provider)
# ---------------------------------------------------------------------------


class _MultiTurnStub:
    """Stub provider that returns tool calls on first call, text on second."""

    def __init__(self) -> None:
        self.call_count = 0

    async def stream_response(
        self, messages_request, *, input_tokens, request_id, thinking_enabled
    ):
        self.call_count += 1
        if self.call_count == 1:
            yield (
                "event: message_start\n"
                'data: {"message":{"id":"msg_1","usage":{"input_tokens":5,'
                '"output_tokens":0}}}\n\n'
            )
            yield (
                "event: content_block_start\n"
                'data: {"index":1,"content_block":{"type":"tool_use",'
                '"id":"fc_1","name":"apply_patch","input":{}}}\n\n'
            )
            yield (
                "event: content_block_delta\n"
                'data: {"index":1,"delta":{"type":"input_json_delta",'
                '"partial_json":"{\\"cmd\\":[\\"apply_patch\\",\\"*** Begin Patch ***\\\\n--- a/x.py\\\\n+++ b/x.py\\\\n@@ -0,0 +1 @@\\\\n+hello\\\\n*** End Patch ***\\"]}"}}\n\n'
            )
            yield 'event: content_block_stop\ndata: {"index":1}\n\n'
            yield 'event: message_delta\ndata: {"usage":{"output_tokens":10}}\n\n'
            yield "event: message_stop\ndata: {}\n\n"
        else:
            yield (
                "event: message_start\n"
                'data: {"message":{"id":"msg_2","usage":{"input_tokens":10,'
                '"output_tokens":0}}}\n\n'
            )
            yield (
                "event: content_block_start\n"
                'data: {"index":0,"content_block":{"type":"text","text":""}}\n\n'
            )
            yield (
                "event: content_block_delta\n"
                'data: {"index":0,"delta":{"type":"text_delta","text":"done"}}\n\n'
            )
            yield 'event: content_block_stop\ndata: {"index":0}\n\n'
            yield 'event: message_delta\ndata: {"usage":{"output_tokens":5}}\n\n'
            yield "event: message_stop\ndata: {}\n\n"


@pytest.fixture
def agent_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "codexproxy")
    monkeypatch.setenv("CODEX_PROXY_AUTH_TOKEN", "codexproxy")
    monkeypatch.setenv("ENABLE_LOCAL_TOOL_EXECUTION", "true")
    monkeypatch.setenv("TOOL_EXECUTION_ALLOWED_COMMANDS", "uv,python,pytest,git")
    monkeypatch.setenv("AGENT_MAX_ITERATIONS", "5")


def _patch_resolve_with_stub(stub: _MultiTurnStub):
    """Return a context manager that patches resolve_provider."""
    return patch("api.dependencies.resolve_provider", return_value=stub)


def test_agent_loop_create_nonstreaming(agent_settings: None) -> None:
    """Agent loop runs tool calls and continues on non-streaming create()."""
    stub = _MultiTurnStub()
    with _patch_resolve_with_stub(stub), TestClient(app) as client:
        resp = client.post(
            "/v1/responses",
            json={
                "model": "gpt-4o",
                "input": [{"type": "message", "role": "user", "content": "hello"}],
                "tools": [{"type": "apply_patch"}],
                "store": False,
            },
            headers={"Authorization": "Bearer codexproxy"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("status") == "completed"
    output = data.get("output", [])
    output_types = [item.get("type") for item in output]
    assert "function_call" in output_types
    assert "function_call_output" in output_types


def test_agent_loop_disabled_by_default() -> None:
    """Without ENABLE_LOCAL_TOOL_EXECUTION, tool calls are returned as-is."""
    stub = _MultiTurnStub()
    with _patch_resolve_with_stub(stub), TestClient(app) as client:
        resp = client.post(
            "/v1/responses",
            json={
                "model": "gpt-4o",
                "input": [{"type": "message", "role": "user", "content": "hello"}],
                "tools": [{"type": "apply_patch"}],
                "store": False,
            },
            headers={"Authorization": "Bearer codexproxy"},
        )
    assert resp.status_code == 200
    data = resp.json()
    output = data.get("output", [])
    output_types = [item.get("type") for item in output]
    assert "function_call" in output_types
    # Without agent loop, tools are NOT executed — no function_call_output
    assert "function_call_output" not in output_types


def test_agent_loop_streaming_returns_events(agent_settings: None) -> None:
    """Streaming path works with the agent loop enabled."""
    stub = _MultiTurnStub()
    with _patch_resolve_with_stub(stub), TestClient(app) as client:
        resp = client.post(
            "/v1/responses",
            json={
                "model": "gpt-4o",
                "input": [{"type": "message", "role": "user", "content": "hello"}],
                "tools": [{"type": "apply_patch"}],
                "stream": True,
                "store": False,
            },
            headers={"Authorization": "Bearer codexproxy"},
        )
    assert resp.status_code == 200
    body = resp.text
    assert "event: response.created" in body
    assert "event: response.completed" in body or "event: response.incomplete" in body


def test_agent_loop_streaming_executes_tools(agent_settings: None) -> None:
    """Streaming agent loop executes tools and emits function_call_output."""
    stub = _MultiTurnStub()
    with _patch_resolve_with_stub(stub), TestClient(app) as client:
        resp = client.post(
            "/v1/responses",
            json={
                "model": "gpt-4o",
                "input": [{"type": "message", "role": "user", "content": "hello"}],
                "tools": [{"type": "apply_patch"}],
                "stream": True,
                "store": False,
            },
            headers={"Authorization": "Bearer codexproxy"},
        )
    assert resp.status_code == 200
    body = resp.text
    assert "event: response.output_item.added" in body
    assert '"function_call_output"' in body
    assert "event: response.completed" in body
