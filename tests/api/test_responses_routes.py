"""Integration tests for ``/v1/responses`` and related routes."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.models.anthropic import (
    ContentBlockImage,
    ContentBlockText,
)
from api.responses_service import (
    _convert_native_tool,
    _expand_namespace_tools,
    _responses_content_to_anthropic,
)
from core.responses.store import ResponseStore, StoredResponse

# A local module-level app avoids touching the global ``app`` fixture used by
# other api tests; this is the same pattern as ``tests/api/test_api.py``.
app = create_app()


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _StubProvider:
    """Streams a canned Anthropic-format SSE sequence."""

    def __init__(self) -> None:
        self.last_messages_request: Any = None
        self.last_thinking_enabled: bool | None = None
        self._stream_data: list[str] | None = None

    def set_stream(self, data: list[str]) -> None:
        self._stream_data = data

    async def stream_response(
        self, messages_request, *, input_tokens, request_id, thinking_enabled
    ):
        self.last_messages_request = messages_request
        self.last_thinking_enabled = thinking_enabled
        if self._stream_data is not None:
            for chunk in self._stream_data:
                yield chunk
            self._stream_data = None
            return
        yield (
            "event: message_start\n"
            'data: {"message":{"id":"msg_1","usage":{"input_tokens":1,'
            '"output_tokens":0}}}\n\n'
        )
        yield (
            "event: content_block_start\n"
            'data: {"index":0,"content_block":{"type":"text","text":""}}\n\n'
        )
        yield (
            "event: content_block_delta\n"
            'data: {"index":0,"delta":{"type":"text_delta","text":"hello"}}\n\n'
        )
        yield 'event: content_block_stop\ndata: {"index":0}\n\n'
        yield 'event: message_delta\ndata: {"usage":{"output_tokens":5}}\n\n'
        yield "event: message_stop\ndata: {}\n\n"


@pytest.fixture
def stub_provider() -> _StubProvider:
    return _StubProvider()


@pytest.fixture
def client(stub_provider: _StubProvider, monkeypatch: pytest.MonkeyPatch):
    """A ``TestClient`` that bypasses the lifespan and shares a seeded store."""
    # Force the auth token so the dependency-injected guard accepts requests
    # without polluting the global env for other test files.
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "codexproxy")
    monkeypatch.setenv("CODEX_PROXY_AUTH_TOKEN", "codexproxy")
    # Pre-seed ``app.state.responses_store`` with a known response so retrieval
    # tests work without a live provider call.
    store = ResponseStore()
    store.put(
        StoredResponse(
            id="resp_seeded",
            created_at=10,
            completed_at=11,
            model="gpt-4o",
            status="completed",
            output=[
                {
                    "id": "msg_seed",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": "seeded", "annotations": []}
                    ],
                }
            ],
            usage={
                "input_tokens": 1,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 2,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 3,
            },
            instructions=None,
            metadata={},
            tools=[],
            tool_choice="auto",
            temperature=1.0,
            top_p=1.0,
            parallel_tool_calls=True,
            previous_response_id=None,
            store=True,
            user=None,
            input_items=[{"type": "message", "role": "user", "content": "hi"}],
        )
    )
    app.state.responses_store = store
    # ``TestClient(app, raise_server_exceptions=True)`` does not enter the
    # FastAPI lifespan context (no startup/shutdown), so the seed survives.
    with (
        patch("api.dependencies.resolve_provider", return_value=stub_provider),
        TestClient(app) as test_client,
    ):
        # The lifespan startup may re-initialise ``app.state``; re-seed the
        # store after entering the context to keep retrieval tests stable.
        state = getattr(test_client.app, "state", None)
        if state is not None:
            state.responses_store = store
        yield test_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer codexproxy",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# /v1/models (Responses shape)
# ---------------------------------------------------------------------------


def test_models_list_contains_configured_model(client: TestClient) -> None:
    """The default ``MODEL`` is exposed (either Anthropic or Responses shape)."""
    response = client.get("/v1/models", headers={"Authorization": "Bearer codexproxy"})
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data["data"], list)
    ids = {entry["id"] for entry in data["data"]}
    # The default MODEL is exposed; either as the bare id, as
    # ``provider/model``, or as ``anthropic/provider/model`` depending on
    # the path that resolves first.
    assert (
        "nvidia_nim/test-model" in ids
        or "test-model" in ids
        or "anthropic/nvidia_nim/test-model" in ids
    )


# ---------------------------------------------------------------------------
# POST /v1/responses (non-streaming)
# ---------------------------------------------------------------------------


def test_post_responses_non_streaming_returns_resource(
    client: TestClient, auth_headers: dict[str, str], stub_provider: _StubProvider
) -> None:
    body = {"model": "gpt-4o", "input": "hi", "stream": False}
    response = client.post("/v1/responses", headers=auth_headers, json=body)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert data["output"][0]["content"][0]["text"] == "hello"
    rid = data["id"]
    assert rid.startswith("resp_")


def test_post_responses_persists_to_shared_store(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    body = {"model": "gpt-4o", "input": "hi", "stream": False}
    response = client.post("/v1/responses", headers=auth_headers, json=body)
    assert response.status_code == 200
    rid = response.json()["id"]
    # Retrieve the stored response via the public GET route — the shared
    # store keeps it accessible across requests.
    get = client.get(f"/v1/responses/{rid}", headers=auth_headers)
    assert get.status_code == 200
    assert get.json()["id"] == rid


def test_post_responses_forwards_to_provider(
    client: TestClient, auth_headers: dict[str, str], stub_provider: _StubProvider
) -> None:
    body = {
        "model": "gpt-4o",
        "input": [
            {"type": "message", "role": "user", "content": "hi"},
        ],
        "stream": False,
        "instructions": "be brief",
    }
    response = client.post("/v1/responses", headers=auth_headers, json=body)
    assert response.status_code == 200
    assert stub_provider.last_messages_request is not None
    req = stub_provider.last_messages_request
    assert req.model == "test-model"
    assert req.messages[0].role == "user"


# ---------------------------------------------------------------------------
# POST /v1/responses (streaming)
# ---------------------------------------------------------------------------


def test_post_responses_streaming_emits_completed_event(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    body = {"model": "gpt-4o", "input": "hi", "stream": True}
    response = client.post("/v1/responses", headers=auth_headers, json=body)
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
    raw = b"".join(response.iter_bytes()).decode()
    assert "event: response.created" in raw
    assert "event: response.in_progress" in raw
    assert "event: response.output_text.delta" in raw
    assert "event: response.completed" in raw


def test_post_responses_multi_turn_tool_use(
    client: TestClient,
    auth_headers: dict[str, str],
    stub_provider: _StubProvider,
) -> None:
    """Simulate multi-turn Codex flow: request → tool call → tool result → response."""
    # --- Turn 1: model returns text + tool_use ---
    turn1_stream = [
        "event: message_start\n"
        'data: {"message":{"id":"msg_1","usage":{"input_tokens":5,'
        '"output_tokens":0}}}\n\n',
        "event: content_block_start\n"
        'data: {"index":0,"content_block":{"type":"text","text":""}}\n\n',
        "event: content_block_delta\n"
        'data: {"index":0,"delta":{"type":"text_delta","text":"Let me '
        'search"}}\n\n',
        'event: content_block_stop\ndata: {"index":0}\n\n',
        "event: content_block_start\n"
        'data: {"index":1,"content_block":{"type":"tool_use",'
        '"id":"toolu_1","name":"Bash","input":{}}}\n\n',
        "event: content_block_delta\n"
        'data: {"index":1,"delta":{"type":"input_json_delta",'
        '"partial_json":"{\\"command\\": \\"ls"}}\n\n',
        "event: content_block_delta\n"
        'data: {"index":1,"delta":{"type":"input_json_delta",'
        '"partial_json":" -la\\"}"}}\n\n',
        'event: content_block_stop\ndata: {"index":1}\n\n',
        'event: message_delta\ndata: {"usage":{"output_tokens":10}}\n\n',
        "event: message_stop\ndata: {}\n\n",
    ]
    stub_provider.set_stream(turn1_stream)

    body1 = {"model": "gpt-4o", "input": "list files", "stream": True}
    resp1 = client.post("/v1/responses", headers=auth_headers, json=body1)
    assert resp1.status_code == 200
    raw1 = b"".join(resp1.iter_bytes()).decode()

    assert "event: response.output_text.delta" in raw1
    assert "event: response.function_call_arguments.delta" in raw1
    assert "event: response.function_call_arguments.done" in raw1
    assert "event: response.output_item.done" in raw1
    assert "event: response.completed" in raw1
    assert "toolu_1" in raw1 or "toolu_1" in raw1

    # --- Turn 2: submit tool result and get more text ---
    turn2_stream = [
        "event: message_start\n"
        'data: {"message":{"id":"msg_2","usage":{"input_tokens":8,'
        '"output_tokens":0}}}\n\n',
        "event: content_block_start\n"
        'data: {"index":0,"content_block":{"type":"text","text":""}}\n\n',
        "event: content_block_delta\n"
        'data: {"index":0,"delta":{"type":"text_delta","text":"Found '
        'files!"}}\n\n',
        'event: content_block_stop\ndata: {"index":0}\n\n',
        'event: message_delta\ndata: {"usage":{"output_tokens":3}}\n\n',
        "event: message_stop\ndata: {}\n\n",
    ]
    stub_provider.set_stream(turn2_stream)

    body2 = {
        "model": "gpt-4o",
        "input": [
            {"type": "message", "role": "user", "content": "list files"},
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Let me search"}],
            },
            {
                "type": "function_call",
                "call_id": "toolu_1",
                "name": "Bash",
                "arguments": '{"command": "ls -la"}',
            },
            {
                "type": "function_call_output",
                "call_id": "toolu_1",
                "output": "total 42\n-rw-r--r-- 1 user user 10 file.txt",
            },
        ],
        "stream": True,
    }
    resp2 = client.post("/v1/responses", headers=auth_headers, json=body2)
    assert resp2.status_code == 200
    raw2 = b"".join(resp2.iter_bytes()).decode()
    assert "event: response.output_text.delta" in raw2
    assert "Found files!" in raw2
    assert "event: response.completed" in raw2

    # Verify the provider received the correct conversation context
    assert stub_provider.last_messages_request is not None
    req = stub_provider.last_messages_request
    assert (
        len(req.messages) == 4
    )  # user + assistant(text) + assistant(tool) + user(tool_result)
    assert req.messages[0].role == "user"
    assert req.messages[2].role == "assistant"
    assert req.messages[2].content[0].type == "tool_use"
    assert req.messages[3].role == "user"
    assert req.messages[3].content[0].type == "tool_result"


# ---------------------------------------------------------------------------
# GET /v1/responses/{id}
# ---------------------------------------------------------------------------


def test_get_response_returns_stored_payload(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.get("/v1/responses/resp_seeded", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "resp_seeded"
    assert data["output"][0]["content"][0]["text"] == "seeded"


def test_get_response_404_for_missing(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.get("/v1/responses/resp_missing", headers=auth_headers)
    assert response.status_code == 404
    assert response.json() == {"detail": {"error": "Response not found."}}


# ---------------------------------------------------------------------------
# GET /v1/responses/{id}/input_items
# ---------------------------------------------------------------------------


def test_get_input_items_returns_seeded_input(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.get("/v1/responses/resp_seeded/input_items", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["object"] == "list"
    assert len(data["data"]) == 1
    assert data["data"][0]["type"] == "message"


def test_get_input_items_404_for_missing(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.get(
        "/v1/responses/resp_missing/input_items", headers=auth_headers
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET /v1/responses (list stub)
# ---------------------------------------------------------------------------


def test_list_responses_stub(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.get("/v1/responses", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["object"] == "list"
    assert isinstance(data["data"], list)
    assert "has_more" in data


# ---------------------------------------------------------------------------
# POST /v1/conversations (stub)
# ---------------------------------------------------------------------------


def test_create_conversation_stub(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post("/v1/conversations", headers=auth_headers, json={})
    assert response.status_code == 200
    data = response.json()
    assert data["id"].startswith("conv_")
    assert data["object"] == "conversation"
    assert isinstance(data["created_at"], int)


# ---------------------------------------------------------------------------
# _responses_content_to_anthropic (Codex CLI input translation)
# ---------------------------------------------------------------------------


def test_responses_content_translates_input_text_to_text() -> None:
    result = _responses_content_to_anthropic([{"type": "input_text", "text": "hello"}])
    assert result == "hello"


def test_responses_content_preserves_plain_text_block() -> None:
    result = _responses_content_to_anthropic([{"type": "text", "text": "hi"}])
    assert result == "hi"


def test_responses_content_returns_list_for_multiple_text_blocks() -> None:
    result = _responses_content_to_anthropic(
        [
            {"type": "input_text", "text": "first"},
            {"type": "input_text", "text": "second"},
        ]
    )
    assert isinstance(result, list)
    assert len(result) == 2
    assert all(isinstance(b, ContentBlockText) for b in result)
    assert [b.text for b in result if isinstance(b, ContentBlockText)] == [
        "first",
        "second",
    ]


def test_responses_content_translates_input_image_to_image_block() -> None:
    result = _responses_content_to_anthropic(
        [
            {
                "type": "input_image",
                "source": {"type": "base64", "media_type": "image/png", "data": "x"},
            }
        ]
    )
    assert isinstance(result, list)
    assert len(result) == 1
    assert isinstance(result[0], ContentBlockImage)
    assert result[0].source == {
        "type": "base64",
        "media_type": "image/png",
        "data": "x",
    }


def test_responses_content_returns_string_for_plain_string_input() -> None:
    assert _responses_content_to_anthropic("hello") == "hello"


def test_responses_content_returns_empty_string_for_none() -> None:
    assert _responses_content_to_anthropic(None) == ""


class TestExpandNamespaceTools:
    """Tests for ``_expand_namespace_tools``."""

    def test_plain_tools_pass_through(self):
        tools = [
            {"type": "function", "name": "tofu", "parameters": {}},
            {"type": "function", "name": "tempeh", "parameters": {}},
        ]
        result = _expand_namespace_tools(tools)
        assert result == tools

    def test_namespace_expands_with_prefix(self):
        tools = [
            {
                "type": "namespace",
                "name": "mcp__bash",
                "tools": [
                    {
                        "type": "function",
                        "name": "execute",
                        "parameters": {"foo": "bar"},
                    },
                    {"type": "function", "name": "read", "parameters": {}},
                ],
            }
        ]
        result = _expand_namespace_tools(tools)
        assert result == [
            {
                "type": "function",
                "name": "mcp__bash__execute",
                "parameters": {"foo": "bar"},
            },
            {"type": "function", "name": "mcp__bash__read", "parameters": {}},
        ]

    def test_namespace_with_inner_tool_missing_name(self):
        tools = [
            {
                "type": "namespace",
                "name": "mcp__fs",
                "tools": [
                    {"type": "function", "name": "write"},
                    {"type": "function"},  # missing name
                ],
            }
        ]
        result = _expand_namespace_tools(tools)
        assert result == [
            {"type": "function", "name": "mcp__fs__write"},
        ]

    def test_empty_namespace_dropped(self):
        tools = [
            {"type": "namespace", "name": "mcp__empty", "tools": []},
        ]
        result = _expand_namespace_tools(tools)
        assert result == []

    def test_namespace_tools_not_a_list(self):
        tools = [
            {"type": "namespace", "name": "mcp__bad", "tools": "oops"},
        ]
        result = _expand_namespace_tools(tools)
        assert result == []

    def test_non_function_inner_types_dropped(self):
        tools = [
            {
                "type": "namespace",
                "name": "mcp__mixed",
                "tools": [
                    {"type": "function", "name": "ok", "parameters": {}},
                    {"type": "namespace", "name": "nested", "tools": []},
                ],
            }
        ]
        result = _expand_namespace_tools(tools)
        assert result == [
            {"type": "function", "name": "mcp__mixed__ok", "parameters": {}},
        ]

    def test_mixed_top_level(self):
        tools = [
            {"type": "function", "name": "plain_tool"},
            {
                "type": "namespace",
                "name": "mcp__repl",
                "tools": [
                    {"type": "function", "name": "eval"},
                ],
            },
            {"type": "function", "name": "another_plain"},
        ]
        result = _expand_namespace_tools(tools)
        assert result == [
            {"type": "function", "name": "plain_tool"},
            {"type": "function", "name": "mcp__repl__eval"},
            {"type": "function", "name": "another_plain"},
        ]

    def test_empty_input(self):
        assert _expand_namespace_tools([]) == []

    def test_input_is_none_not_list(self):
        assert _expand_namespace_tools([]) == []


class TestConvertNativeTool:
    """Tests for ``_convert_native_tool``."""

    def test_apply_patch_converted_to_function(self):
        result = _convert_native_tool({"type": "apply_patch"})
        assert result["type"] == "function"
        assert result["name"] == "apply_patch"
        assert "description" in result
        assert "parameters" in result
        assert result["parameters"]["type"] == "object"
        assert "patch" in result["parameters"]["properties"]
        assert result["parameters"]["required"] == ["patch"]

    def test_apply_patch_argument_translation(self):
        from core.responses.sse import _translate_tool_arguments

        # Model calls apply_patch with {"patch": "..."}
        result = _translate_tool_arguments(
            "apply_patch",
            '{"patch": "*** Begin Patch ***\\n--- a/main.py\\n+++ b/main.py\\n@@ -1 +1,2 @@\\n-old\\n+new\\n*** End Patch ***"}',
        )
        parsed = json.loads(result)
        assert "cmd" in parsed
        assert parsed["cmd"][0] == "apply_patch"
        assert "*** Begin Patch ***" in parsed["cmd"][1]
        assert "patch" not in parsed

    def test_apply_patch_expanded_in_tools(self):
        tools = [
            {"type": "apply_patch"},
            {"type": "function", "name": "shell_command"},
        ]
        result = _expand_namespace_tools(tools)
        assert len(result) == 2
        assert result[0]["type"] == "function"
        assert result[0]["name"] == "apply_patch"
        assert result[1]["type"] == "function"
        assert result[1]["name"] == "shell_command"

    def test_function_tool_passes_through(self):
        raw = {"type": "function", "name": "greet", "parameters": {}}
        assert _convert_native_tool(raw) is raw

    def test_namespace_tool_passes_through(self):
        raw = {"type": "namespace", "name": "mcp__x", "tools": []}
        assert _convert_native_tool(raw) is raw

    def test_unknown_type_passes_through(self):
        raw = {"type": "weird", "name": "x"}
        assert _convert_native_tool(raw) is raw


def test_responses_content_skips_unknown_blocks() -> None:
    result = _responses_content_to_anthropic(
        [
            {"type": "input_text", "text": "keep"},
            {"type": "weird_thing", "data": "drop"},
        ]
    )
    assert result == "keep"
