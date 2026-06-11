"""Tests for the ``/v1/chat/completions`` endpoint and adapter functions."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.openai_compat import (
    _chat_message_to_input_item,
    _chat_tool_to_responses_tool,
    _extract_chat_response,
    _map_finish_reason,
    _responses_sse_to_chat_stream,
    chat_to_responses_request,
)
from config.settings import get_settings
from providers.registry import ProviderRegistry

app = create_app()


class _StubProvider:
    """Streams a canned Responses-format SSE sequence."""

    def __init__(self) -> None:
        self.last_messages_request: Any = None
        self._stream_data: list[str] | None = None

    def set_stream(self, data: list[str]) -> None:
        self._stream_data = data

    async def stream_response(
        self, messages_request, *, input_tokens, request_id, thinking_enabled
    ):
        self.last_messages_request = messages_request
        if self._stream_data is not None:
            for chunk in self._stream_data:
                yield chunk

    async def create_response(self, messages_request, **kwargs):
        self.last_messages_request = messages_request
        if self._stream_data is not None:
            return {
                "id": "resp_test",
                "object": "response",
                "created_at": 100,
                "model": "gpt-4o",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Hello!"}],
                    }
                ],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                },
            }


@pytest.fixture
def stub_provider() -> _StubProvider:
    return _StubProvider()


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer codexproxy"}


# ------------------------------------------------------------------
# Unit tests: message conversion
# ------------------------------------------------------------------


class TestChatMessageToInputItem:
    def test_user_message(self) -> None:
        result = _chat_message_to_input_item({"role": "user", "content": "Hello"})
        assert result["type"] == "message"
        assert result["role"] == "user"
        assert result["content"] == [{"type": "input_text", "text": "Hello"}]

    def test_system_message(self) -> None:
        result = _chat_message_to_input_item(
            {"role": "system", "content": "Be helpful"}
        )
        assert result["type"] == "message"
        assert result["role"] == "system"

    def test_assistant_message_text_only(self) -> None:
        result = _chat_message_to_input_item({"role": "assistant", "content": "Sure!"})
        assert result["type"] == "message"
        assert result["role"] == "assistant"
        assert result["content"] == [{"type": "input_text", "text": "Sure!"}]

    def test_assistant_message_with_tool_calls(self) -> None:
        result = _chat_message_to_input_item(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "apply_patch",
                            "arguments": '{"patch": "test"}',
                        },
                    }
                ],
            }
        )
        assert result["type"] == "message"
        assert result["role"] == "assistant"
        assert len(result["content"]) == 1
        fc = result["content"][0]
        assert fc["type"] == "function_call"
        assert fc["call_id"] == "call_1"
        assert fc["name"] == "apply_patch"
        assert fc["arguments"] == '{"patch": "test"}'

    def test_tool_message(self) -> None:
        result = _chat_message_to_input_item(
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": '{"ok": true}',
            }
        )
        assert result["type"] == "function_call_output"
        assert result["call_id"] == "call_1"
        assert result["output"] == '{"ok": true}'


# ------------------------------------------------------------------
# Unit tests: tool conversion
# ------------------------------------------------------------------


class TestChatToolToResponsesTool:
    def test_function_tool(self) -> None:
        t = {
            "type": "function",
            "function": {
                "name": "apply_patch",
                "description": "Apply a patch",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        result = _chat_tool_to_responses_tool(t)
        assert result["type"] == "function"
        assert result["name"] == "apply_patch"
        assert result["description"] == "Apply a patch"

    def test_unknown_tool_passthrough(self) -> None:
        t = {"type": "custom", "name": "my_tool"}
        result = _chat_tool_to_responses_tool(t)
        assert result == t


# ------------------------------------------------------------------
# Unit tests: request conversion
# ------------------------------------------------------------------


class TestChatToResponsesRequest:
    def test_basic_conversion(self) -> None:
        body: dict[str, Any] = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "temperature": 0.7,
            "max_tokens": 100,
            "stream": False,
        }
        req = chat_to_responses_request(body)
        assert req.model == "gpt-4o"
        assert req.temperature == 0.7
        assert req.max_output_tokens == 100
        assert req.stream is False

    def test_tool_choice_none(self) -> None:
        body: dict[str, Any] = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hi"}],
            "tool_choice": "none",
        }
        req = chat_to_responses_request(body)
        assert req.tool_choice == "none"

    def test_tool_choice_function(self) -> None:
        body: dict[str, Any] = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hi"}],
            "tool_choice": {
                "type": "function",
                "function": {"name": "apply_patch"},
            },
        }
        req = chat_to_responses_request(body)
        assert req.tool_choice == {"type": "function", "name": "apply_patch"}

    def test_with_tools(self) -> None:
        body: dict[str, Any] = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "apply_patch",
                        "description": "Apply a patch",
                        "parameters": {},
                    },
                }
            ],
        }
        req = chat_to_responses_request(body)
        assert len(req.tools) == 1
        tool = req.tools[0]
        name = tool.name if hasattr(tool, "name") else tool["name"]
        assert name == "apply_patch"


# ------------------------------------------------------------------
# Unit tests: response extraction
# ------------------------------------------------------------------


class TestExtractChatResponse:
    def test_basic_extraction(self) -> None:
        resp: dict[str, Any] = {
            "id": "resp_abc",
            "created_at": 100,
            "model": "gpt-4o",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Hello!"}],
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result = _extract_chat_response(resp)
        assert result["object"] == "chat.completion"
        assert result["id"] == "resp_abc"
        assert result["choices"][0]["message"]["content"] == "Hello!"
        assert result["choices"][0]["finish_reason"] == "stop"
        assert "usage" in result

    def test_empty_output_fallback(self) -> None:
        resp: dict[str, Any] = {
            "id": "resp_xyz",
            "created_at": 200,
            "model": "gpt-4o",
            "status": "completed",
            "output": [],
        }
        result = _extract_chat_response(resp)
        assert len(result["choices"]) == 1
        assert result["choices"][0]["message"]["content"] is None

    def test_function_call_output(self) -> None:
        resp: dict[str, Any] = {
            "id": "resp_func",
            "created_at": 300,
            "model": "gpt-4o",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "function_call",
                            "call_id": "call_1",
                            "name": "apply_patch",
                            "arguments": '{"patch": "test"}',
                        }
                    ],
                }
            ],
        }
        result = _extract_chat_response(resp)
        assert len(result["choices"]) == 1
        msg = result["choices"][0]["message"]
        assert msg.get("content") is None
        assert msg["tool_calls"][0]["function"]["name"] == "apply_patch"

    def test_incomplete_status(self) -> None:
        resp: dict[str, Any] = {
            "id": "resp_inc",
            "created_at": 400,
            "model": "gpt-4o",
            "status": "incomplete",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Partial"}],
                }
            ],
        }
        result = _extract_chat_response(resp)
        assert result["choices"][0]["finish_reason"] == "length"


# ------------------------------------------------------------------
# Unit tests: finish reason mapping
# ------------------------------------------------------------------


class TestMapFinishReason:
    def test_completed(self) -> None:
        assert _map_finish_reason("completed") == "stop"

    def test_incomplete(self) -> None:
        assert _map_finish_reason("incomplete") == "length"

    def test_failed(self) -> None:
        assert _map_finish_reason("failed") == "error"

    def test_unknown(self) -> None:
        assert _map_finish_reason("unknown") == "stop"


# ------------------------------------------------------------------
# Unit tests: streaming converter (async)
# ------------------------------------------------------------------


class TestResponsesSseToChatStream:
    @pytest.mark.asyncio
    async def test_yields_text_deltas(self) -> None:
        chunks = [
            'data: {"type":"response.created","response":{"model":"gpt-4o"}}\n\n',
            'data: {"type":"response.output_text.delta","output_index":0,"delta":"Hel"}\n\n',
            'data: {"type":"response.output_text.delta","output_index":0,"delta":"lo"}\n\n',
            'data: {"type":"response.completed","response":{"status":"completed"}}\n\n',
        ]

        async def _stream():
            for c in chunks:
                yield c

        results = [
            msg.strip() async for msg in _responses_sse_to_chat_stream(_stream())
        ]

        assert len(results) == 4
        assert results[-1] == "data: [DONE]"

        # First content delta is at index 0, second at index 1
        delta0 = results[0]
        assert '"content": "Hel"' in delta0
        assert '"finish_reason": null' in delta0

    @pytest.mark.asyncio
    async def test_yields_function_call_deltas(self) -> None:
        chunks = [
            'data: {"type":"response.created","response":{"model":"gpt-4o"}}\n\n',
            'data: {"type":"response.function_call_arguments.delta","output_index":0,"delta":"{\\"pa"}\n\n',
            'data: {"type":"response.completed"}\n\n',
        ]

        async def _stream():
            for c in chunks:
                yield c

        results = [
            msg.strip() async for msg in _responses_sse_to_chat_stream(_stream())
        ]

        assert len(results) == 3  # function delta + completed + [DONE]
        assert '"function": {"arguments": "{\\"pa"' in results[0]

    @pytest.mark.asyncio
    async def test_completed_yields_finish_reason(self) -> None:
        chunks = [
            'data: {"type":"response.created"}\n\n',
            'data: {"type":"response.completed"}\n\n',
        ]

        async def _stream():
            for c in chunks:
                yield c

        results = [
            msg.strip() async for msg in _responses_sse_to_chat_stream(_stream())
        ]

        assert len(results) == 2  # completed chunk + [DONE]
        finish = json.loads(results[0].removeprefix("data: "))
        assert finish["choices"][0]["finish_reason"] == "stop"

    @pytest.mark.asyncio
    async def test_incomplete_yields_length_reason(self) -> None:
        chunks = [
            'data: {"type":"response.created"}\n\n',
            'data: {"type":"response.incomplete"}\n\n',
        ]

        async def _stream():
            for c in chunks:
                yield c

        results = [
            msg.strip() async for msg in _responses_sse_to_chat_stream(_stream())
        ]

        assert len(results) == 2  # incomplete chunk + [DONE]
        finish = json.loads(results[0].removeprefix("data: "))
        assert finish["choices"][0]["finish_reason"] == "length"

    @pytest.mark.asyncio
    async def test_skips_non_data_lines(self) -> None:
        chunks = [
            "event: some_event\n",
            'data: {"type":"response.created"}\n\n',
            ":comment\n",
            'data: {"type":"response.completed"}\n\n',
        ]

        async def _stream():
            for c in chunks:
                yield c

        results = [
            msg.strip() async for msg in _responses_sse_to_chat_stream(_stream())
        ]

        assert len(results) == 2  # completed chunk + [DONE]


# ------------------------------------------------------------------
# Integration tests: /v1/chat/completions route
# ------------------------------------------------------------------


class TestChatCompletionsRoute:
    @pytest.fixture(autouse=True)
    def _setup(self, stub_provider: _StubProvider, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "codexproxy")
        monkeypatch.setenv("CODEX_PROXY_AUTH_TOKEN", "codexproxy")
        get_settings.cache_clear()
        self._stub = stub_provider

    def test_non_streaming_returns_chat_response(self, auth_headers):
        self._stub._stream_data = [
            'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","role":"assistant","content":[],"model":"gpt-4o","usage":{"input_tokens":10,"output_tokens":5}}}\n\n',
            'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
            'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello!"}}\n\n',
            'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
            'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":5}}\n\n',
            'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ]

        with (
            patch.object(
                ProviderRegistry, "validate_configured_models", new=AsyncMock()
            ),
            patch("api.responses_routes._resolve_provider", return_value=self._stub),
            TestClient(app) as client,
        ):
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
                headers=auth_headers,
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "chat.completion"
        assert len(body["choices"]) == 1
        assert body["choices"][0]["message"]["content"] == "Hello!"
        assert body["choices"][0]["message"]["role"] == "assistant"

    def test_streaming_returns_sse_stream(self, auth_headers):
        self._stub._stream_data = [
            'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","role":"assistant","content":[],"model":"gpt-4o","usage":{"input_tokens":10,"output_tokens":0}}}\n\n',
            'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
            'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}\n\n',
            'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
            'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":5}}\n\n',
            'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ]

        with (
            patch.object(
                ProviderRegistry, "validate_configured_models", new=AsyncMock()
            ),
            patch("api.responses_routes._resolve_provider", return_value=self._stub),
            TestClient(app) as client,
        ):
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "stream": True,
                },
                headers=auth_headers,
            )

        assert resp.status_code == 200
        assert resp.headers.get("content-type", "").startswith("text/event-stream")
        text = resp.text
        assert "data: [DONE]" in text
        assert "Hello" in text

    def test_with_tools(self, auth_headers):
        self._stub._stream_data = [
            'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","role":"assistant","content":[],"model":"gpt-4o","usage":{"input_tokens":20,"output_tokens":1}}}\n\n',
            'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"call_1","name":"apply_patch","input":{}}}\n\n',
            'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
            'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":1}}\n\n',
            'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ]

        with (
            patch.object(
                ProviderRegistry, "validate_configured_models", new=AsyncMock()
            ),
            patch("api.responses_routes._resolve_provider", return_value=self._stub),
            TestClient(app) as client,
        ):
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "Edit file"}],
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "apply_patch",
                                "description": "Apply a patch",
                                "parameters": {"type": "object", "properties": {}},
                            },
                        }
                    ],
                },
                headers=auth_headers,
            )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["choices"][0]["message"]["tool_calls"]) == 1
        tool_call = body["choices"][0]["message"]["tool_calls"][0]
        assert tool_call["function"]["name"] == "apply_patch"

    def test_tool_choice_none(self, auth_headers):
        self._stub._stream_data = [
            'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","role":"assistant","content":[],"model":"gpt-4o","usage":{"input_tokens":5,"output_tokens":1}}}\n\n',
            'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
            'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
            'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}\n\n',
            'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ]

        with (
            patch.object(
                ProviderRegistry, "validate_configured_models", new=AsyncMock()
            ),
            patch("api.responses_routes._resolve_provider", return_value=self._stub),
            TestClient(app) as client,
        ):
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "tool_choice": "none",
                },
                headers=auth_headers,
            )

        assert resp.status_code == 200

    def test_missing_auth_returns_401(self) -> None:
        with (
            patch.object(
                ProviderRegistry, "validate_configured_models", new=AsyncMock()
            ),
            TestClient(app) as client,
        ):
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )
        assert resp.status_code == 401

    def test_parameter_passthrough(self, stub_provider, auth_headers):
        stub_provider._stream_data = [
            'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","role":"assistant","content":[],"model":"gpt-4o","usage":{"input_tokens":5,"output_tokens":1}}}\n\n',
            'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
            'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
            'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}\n\n',
            'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ]

        with (
            patch.object(
                ProviderRegistry, "validate_configured_models", new=AsyncMock()
            ),
            patch("api.responses_routes._resolve_provider", return_value=stub_provider),
            TestClient(app) as client,
        ):
            client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "temperature": 0.5,
                    "max_tokens": 200,
                    "top_p": 0.9,
                    "user": "test_user",
                },
                headers=auth_headers,
            )

        assert stub_provider.last_messages_request is not None
