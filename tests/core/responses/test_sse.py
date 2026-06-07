"""Unit tests for the Responses SSE builders and the Anthropic adapter."""

from __future__ import annotations

import json

from core.responses.sse import (
    AnthropicToResponsesAdapter,
    build_content_part_added,
    build_content_part_done,
    build_function_call_arguments_delta,
    build_function_call_arguments_done,
    build_output_item_added,
    build_output_item_done,
    build_output_text_delta,
    build_output_text_done,
    build_reasoning_summary_delta,
    build_reasoning_summary_done,
    build_response_completed,
    build_response_created,
    build_response_in_progress,
    format_responses_sse,
    new_call_id,
    new_function_call_id,
    new_output_item_id,
    new_reasoning_id,
    new_response_id,
)

# ---------------------------------------------------------------------------
# New-id helpers
# ---------------------------------------------------------------------------


def test_new_response_id_uses_resp_prefix() -> None:
    rid = new_response_id()
    assert rid.startswith("resp_")
    assert len(rid) == len("resp_") + 32


def test_new_output_item_id_uses_msg_prefix() -> None:
    oid = new_output_item_id()
    assert oid.startswith("msg_")
    assert len(oid) == len("msg_") + 32


def test_new_function_call_id_uses_fc_prefix() -> None:
    fid = new_function_call_id()
    assert fid.startswith("fc_")
    assert len(fid) == len("fc_") + 32


def test_new_call_id_uses_call_prefix() -> None:
    cid = new_call_id()
    assert cid.startswith("call_")
    assert len(cid) == len("call_") + 32


def test_new_reasoning_id_uses_rs_prefix() -> None:
    rid = new_reasoning_id()
    assert rid.startswith("rs_")
    assert len(rid) == len("rs_") + 32


def test_new_ids_are_unique() -> None:
    assert new_response_id() != new_response_id()
    assert new_output_item_id() != new_output_item_id()


# ---------------------------------------------------------------------------
# format_responses_sse
# ---------------------------------------------------------------------------


def test_format_responses_sse_produces_event_and_data_lines() -> None:
    chunk = format_responses_sse(
        "response.created", {"type": "response.created", "x": 1}
    )
    assert chunk.startswith("event: response.created\n")
    assert "data: " in chunk
    assert chunk.endswith("\n\n")
    data_line = chunk.split("data: ", 1)[1].split("\n", 1)[0]
    assert json.loads(data_line) == {"type": "response.created", "x": 1}


# ---------------------------------------------------------------------------
# Event builders
# ---------------------------------------------------------------------------


def test_build_response_created_emits_in_progress_status() -> None:
    chunk = build_response_created(
        response_id="resp_1",
        created_at=10,
        model="gpt-4o",
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
    )
    payload = _data_of(chunk)
    assert payload["type"] == "response.created"
    assert payload["response"]["id"] == "resp_1"
    assert payload["response"]["status"] == "in_progress"
    assert payload["response"]["model"] == "gpt-4o"
    assert payload["response"]["output"] == []


def test_build_response_in_progress() -> None:
    chunk = build_response_in_progress("resp_1")
    payload = _data_of(chunk)
    assert payload["type"] == "response.in_progress"
    assert payload["response"]["status"] == "in_progress"


def test_build_output_item_added() -> None:
    item = {"id": "msg_1", "type": "message", "role": "assistant"}
    chunk = build_output_item_added(item, 0)
    payload = _data_of(chunk)
    assert payload["output_index"] == 0
    assert payload["item"] == item


def test_build_content_part_added() -> None:
    chunk = build_content_part_added(
        item_id="msg_1",
        output_index=0,
        content_index=0,
        part={"type": "output_text", "text": ""},
    )
    payload = _data_of(chunk)
    assert payload["item_id"] == "msg_1"
    assert payload["part"]["type"] == "output_text"


def test_build_output_text_delta() -> None:
    chunk = build_output_text_delta(
        item_id="msg_1", output_index=0, content_index=0, delta="hi"
    )
    payload = _data_of(chunk)
    assert payload["delta"] == "hi"
    assert payload["logprobs"] == []


def test_build_output_text_done() -> None:
    chunk = build_output_text_done(
        item_id="msg_1", output_index=0, content_index=0, text="hi"
    )
    payload = _data_of(chunk)
    assert payload["text"] == "hi"


def test_build_content_part_done() -> None:
    chunk = build_content_part_done(
        item_id="msg_1",
        output_index=0,
        content_index=0,
        part={"type": "output_text", "text": "hi"},
    )
    payload = _data_of(chunk)
    assert payload["part"]["text"] == "hi"


def test_build_function_call_arguments_delta_and_done() -> None:
    delta = build_function_call_arguments_delta(
        item_id="fc_1", output_index=0, delta='{"x":'
    )
    done = build_function_call_arguments_done(
        item_id="fc_1", output_index=0, arguments='{"x":1}'
    )
    assert _data_of(delta)["delta"] == '{"x":'
    assert _data_of(done)["arguments"] == '{"x":1}'


def test_build_reasoning_summary_delta() -> None:
    chunk = build_reasoning_summary_delta(item_id="rs_1", delta="I think...")
    payload = _data_of(chunk)
    assert payload["type"] == "response.reasoning_summary.delta"
    assert payload["item_id"] == "rs_1"
    assert payload["delta"] == "I think..."


def test_build_reasoning_summary_done() -> None:
    summary = [{"type": "summary_text", "text": "I think..."}]
    chunk = build_reasoning_summary_done(item_id="rs_1", summary=summary)
    payload = _data_of(chunk)
    assert payload["type"] == "response.reasoning_summary.done"
    assert payload["item_id"] == "rs_1"
    assert payload["summary"] == summary


def test_build_output_item_done() -> None:
    chunk = build_output_item_done(
        {"id": "msg_1", "type": "message", "role": "assistant", "status": "completed"},
        0,
    )
    payload = _data_of(chunk)
    assert payload["item"]["status"] == "completed"


def test_build_response_completed_includes_output_and_usage() -> None:
    chunk = build_response_completed(
        response_id="resp_1",
        created_at=10,
        completed_at=11,
        model="gpt-4o",
        output=[
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "hi", "annotations": []}],
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
    )
    payload = _data_of(chunk)
    assert payload["type"] == "response.completed"
    assert payload["response"]["status"] == "completed"
    assert payload["response"]["output"][0]["content"][0]["text"] == "hi"
    assert payload["response"]["usage"]["total_tokens"] == 3


def test_build_response_completed_emits_incomplete_when_status_failed() -> None:
    chunk = build_response_completed(
        response_id="resp_1",
        created_at=10,
        completed_at=11,
        model="gpt-4o",
        output=[],
        usage={
            "input_tokens": 0,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 0,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 0,
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
        status="incomplete",
    )
    assert _data_of(chunk)["type"] == "response.incomplete"


def _data_of(chunk: str) -> dict:
    line = chunk.split("data: ", 1)[1].split("\n", 1)[0]
    return json.loads(line)


# ---------------------------------------------------------------------------
# AnthropicToResponsesAdapter
# ---------------------------------------------------------------------------


def _adapter() -> AnthropicToResponsesAdapter:
    return AnthropicToResponsesAdapter(
        response_id="resp_abc",
        created_at=1,
        model="gpt-4o",
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
    )


def _evtype(chunk: str) -> str:
    return chunk.split("event: ", 1)[1].split("\n", 1)[0]


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def test_opening_events_emits_created_and_in_progress() -> None:
    adapter = _adapter()
    events = list(adapter.opening_events())
    assert _evtype(events[0]) == "response.created"
    assert _evtype(events[1]) == "response.in_progress"


def test_feed_emits_full_text_lifecycle() -> None:
    adapter = _adapter()
    list(adapter.opening_events())
    stream = "".join(
        [
            _sse(
                "message_start",
                {"message": {"usage": {"input_tokens": 1, "output_tokens": 0}}},
            ),
            _sse(
                "content_block_start",
                {"index": 0, "content_block": {"type": "text", "text": ""}},
            ),
            _sse(
                "content_block_delta",
                {"index": 0, "delta": {"type": "text_delta", "text": "Hi"}},
            ),
            _sse("content_block_stop", {"index": 0}),
            _sse("message_delta", {"usage": {"output_tokens": 3}}),
            _sse("message_stop", {}),
        ]
    )
    events = list(adapter.feed(stream)) + list(adapter.finalize())
    types = [_evtype(e) for e in events]
    assert "response.output_text.delta" in types
    assert "response.output_text.done" in types
    assert "response.content_part.done" in types
    assert "response.output_item.done" in types
    assert types[-1] == "response.completed"
    assert adapter.status == "completed"
    assert adapter.usage["output_tokens"] == 3
    text = adapter.output[0]["content"][0]["text"]
    assert text == "Hi"


def test_feed_emits_thinking_then_text_lifecycle() -> None:
    """Thinking block is emitted as a reasoning item, then text as a message."""
    adapter = _adapter()
    list(adapter.opening_events())
    stream = "".join(
        [
            _sse(
                "message_start",
                {"message": {"usage": {"input_tokens": 1, "output_tokens": 0}}},
            ),
            _sse(
                "content_block_start",
                {"index": 0, "content_block": {"type": "thinking", "thinking": ""}},
            ),
            _sse(
                "content_block_delta",
                {
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "I think..."},
                },
            ),
            _sse("content_block_stop", {"index": 0}),
            _sse(
                "content_block_start",
                {"index": 1, "content_block": {"type": "text", "text": ""}},
            ),
            _sse(
                "content_block_delta",
                {"index": 1, "delta": {"type": "text_delta", "text": "Answer"}},
            ),
            _sse("content_block_stop", {"index": 1}),
            _sse("message_delta", {"usage": {"output_tokens": 10}}),
            _sse("message_stop", {}),
        ]
    )
    events = list(adapter.feed(stream)) + list(adapter.finalize())
    types = [_evtype(e) for e in events]
    assert "response.reasoning_summary.delta" in types
    assert "response.reasoning_summary.done" in types
    assert "response.output_text.delta" in types
    assert "response.output_text.done" in types
    assert types[-1] == "response.completed"
    # Two output items: reasoning (index 0) and message (index 1)
    reasoning_items = [it for it in adapter.output if it["type"] == "reasoning"]
    message_items = [it for it in adapter.output if it["type"] == "message"]
    assert len(reasoning_items) == 1
    assert len(message_items) == 1
    assert reasoning_items[0]["summary"][0]["text"] == "I think..."
    assert message_items[0]["content"][0]["text"] == "Answer"


def test_feed_thinking_only() -> None:
    """Thinking-only output still produces reasoning item and a placeholder."""
    adapter = _adapter()
    list(adapter.opening_events())
    stream = "".join(
        [
            _sse(
                "message_start",
                {"message": {"usage": {"input_tokens": 1, "output_tokens": 0}}},
            ),
            _sse(
                "content_block_start",
                {"index": 0, "content_block": {"type": "thinking", "thinking": ""}},
            ),
            _sse(
                "content_block_delta",
                {
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "Only reasoning"},
                },
            ),
            _sse("content_block_stop", {"index": 0}),
            _sse("message_delta", {"usage": {"output_tokens": 5}}),
            _sse("message_stop", {}),
        ]
    )
    events = list(adapter.feed(stream)) + list(adapter.finalize())
    types = [_evtype(e) for e in events]
    assert "response.reasoning_summary.delta" in types
    assert "response.reasoning_summary.done" in types
    assert types[-1] == "response.completed"
    reasoning_items = [it for it in adapter.output if it["type"] == "reasoning"]
    assert len(reasoning_items) == 1
    assert reasoning_items[0]["summary"][0]["text"] == "Only reasoning"


def test_feed_emits_function_call_block() -> None:
    adapter = _adapter()
    list(adapter.opening_events())
    stream = "".join(
        [
            _sse(
                "message_start",
                {"message": {"usage": {"input_tokens": 1, "output_tokens": 0}}},
            ),
            _sse(
                "content_block_start",
                {
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "fc_xyz",
                        "name": "get_weather",
                        "input": {},
                    },
                },
            ),
            _sse(
                "content_block_delta",
                {
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '{"city":'},
                },
            ),
            _sse(
                "content_block_delta",
                {
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '"Paris"}'},
                },
            ),
            _sse("content_block_stop", {"index": 0}),
            _sse("message_stop", {}),
        ]
    )
    events = list(adapter.feed(stream)) + list(adapter.finalize())
    types = [_evtype(e) for e in events]
    assert "response.function_call_arguments.delta" in types
    assert "response.function_call_arguments.done" in types
    assert types[-1] == "response.completed"
    fc = next(item for item in adapter.output if item["type"] == "function_call")
    assert fc["name"] == "get_weather"
    assert json.loads(fc["arguments"]) == {"city": "Paris"}


def test_feed_records_error_status() -> None:
    adapter = _adapter()
    list(adapter.opening_events())
    stream = _sse(
        "error",
        {"type": "error", "error": {"type": "overloaded", "message": "x"}},
    )
    list(adapter.feed(stream))
    events = list(adapter.finalize())
    assert _evtype(events[-1]) == "response.incomplete"
    assert adapter.status == "failed"


def test_finalize_is_idempotent() -> None:
    adapter = _adapter()
    list(adapter.opening_events())
    stream = "".join(
        [
            _sse(
                "message_start",
                {"message": {"usage": {"input_tokens": 1, "output_tokens": 0}}},
            ),
            _sse(
                "content_block_start",
                {"index": 0, "content_block": {"type": "text", "text": ""}},
            ),
            _sse(
                "content_block_delta",
                {"index": 0, "delta": {"type": "text_delta", "text": "x"}},
            ),
            _sse("content_block_stop", {"index": 0}),
            _sse("message_stop", {}),
        ]
    )
    list(adapter.feed(stream))
    first = list(adapter.finalize())
    second = list(adapter.finalize())
    # First ``finalize()`` emits the terminal ``response.completed`` event.
    assert len(first) == 1
    assert _evtype(first[0]) == "response.completed"
    # Second ``finalize()`` is a no-op because the terminal event was already emitted.
    assert second == []


def test_finalize_closes_unclosed_blocks() -> None:
    adapter = _adapter()
    list(adapter.opening_events())
    # No ``content_block_stop`` and no ``message_stop`` — simulate a truncated stream.
    stream = "".join(
        [
            _sse(
                "message_start",
                {"message": {"usage": {"input_tokens": 1, "output_tokens": 0}}},
            ),
            _sse(
                "content_block_start",
                {"index": 0, "content_block": {"type": "text", "text": ""}},
            ),
            _sse(
                "content_block_delta",
                {"index": 0, "delta": {"type": "text_delta", "text": "ok"}},
            ),
        ]
    )
    list(adapter.feed(stream))
    types = [_evtype(e) for e in adapter.finalize()]
    assert "response.output_text.done" in types
    assert "response.content_part.done" in types
    assert "response.output_item.done" in types
    assert "response.completed" in types


def test_finalize_closes_unclosed_reasoning_block() -> None:
    adapter = _adapter()
    list(adapter.opening_events())
    stream = "".join(
        [
            _sse(
                "message_start",
                {"message": {"usage": {"input_tokens": 1, "output_tokens": 0}}},
            ),
            _sse(
                "content_block_start",
                {"index": 0, "content_block": {"type": "thinking", "thinking": ""}},
            ),
            _sse(
                "content_block_delta",
                {
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "truncated reasoning"},
                },
            ),
            # No content_block_stop — simulate truncation mid-thinking
        ]
    )
    list(adapter.feed(stream))
    events = list(adapter.finalize())
    types = [_evtype(e) for e in events]
    assert "response.reasoning_summary.done" in types
    assert "response.output_item.done" in types
    assert "response.completed" in types
    reasoning_items = [it for it in adapter.output if it["type"] == "reasoning"]
    assert len(reasoning_items) == 1
    assert reasoning_items[0]["summary"][0]["text"] == "truncated reasoning"


def test_output_includes_reasoning_items() -> None:
    adapter = _adapter()
    list(adapter.opening_events())
    stream = "".join(
        [
            _sse(
                "message_start",
                {"message": {"usage": {"input_tokens": 1, "output_tokens": 0}}},
            ),
            _sse(
                "content_block_start",
                {"index": 0, "content_block": {"type": "thinking", "thinking": ""}},
            ),
            _sse(
                "content_block_delta",
                {
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "r1"},
                },
            ),
            _sse("content_block_stop", {"index": 0}),
            _sse("message_stop", {}),
        ]
    )
    list(adapter.feed(stream)) + list(adapter.finalize())
    ids = {it["type"] for it in adapter.output}
    assert "reasoning" in ids


def test_feed_ignores_unknown_event_types() -> None:
    adapter = _adapter()
    list(adapter.opening_events())
    stream = _sse("ping", {})
    events = list(adapter.feed(stream))
    assert events == []
