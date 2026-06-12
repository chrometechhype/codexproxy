"""OpenAI Chat Completions adapter for the Responses API surface.

Translates ``POST /v1/chat/completions`` requests into the internal
``/v1/responses`` format and converts responses back to the OpenAI
Chat Completions schema.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from api.models.responses import ResponsesCreateRequest

# ---------------------------------------------------------------------------
# Request conversion  (chat.completions -> ResponsesCreateRequest)
# ---------------------------------------------------------------------------


def _chat_message_to_input_item(msg: dict[str, Any]) -> dict[str, Any]:
    """Convert a single chat message to a Responses input item."""
    role = msg.get("role", "user")
    content = msg.get("content", "")
    if role == "assistant":
        tool_calls = msg.get("tool_calls")
        item: dict[str, Any] = {"type": "message", "role": "assistant"}
        content_list: list[dict[str, Any]] = []
        if content:
            content_list.append({"type": "input_text", "text": content})
        if tool_calls:
            for tc in tool_calls:
                func = tc.get("function", {})
                content_list.append(
                    {
                        "type": "function_call",
                        "call_id": tc.get("id", f"fc_{uuid.uuid4().hex[:12]}"),
                        "name": func.get("name", ""),
                        "arguments": func.get("arguments", "{}"),
                    }
                )
        item["content"] = content_list
        return item

    if role == "tool":
        return {
            "type": "function_call_output",
            "call_id": msg.get("tool_call_id", ""),
            "output": msg.get("content", ""),
        }

    # user / system
    text = content if isinstance(content, str) else (content or "")
    return {
        "type": "message",
        "role": role,
        "content": [{"type": "input_text", "text": text}],
    }


def _chat_tool_to_responses_tool(t: dict[str, Any]) -> dict[str, Any]:
    """Convert an OpenAI chat tool to a Responses tool."""
    if t.get("type") == "function":
        func = t.get("function", {})
        return {
            "type": "function",
            "name": func.get("name", ""),
            "description": func.get("description", ""),
            "parameters": func.get("parameters", {}),
        }
    return t


def chat_to_responses_request(body: dict[str, Any]) -> ResponsesCreateRequest:
    """Build a ``ResponsesCreateRequest`` from a Chat Completions body."""
    raw_messages = body.get("messages", [])
    messages = [_chat_message_to_input_item(m) for m in raw_messages]
    tools = [_chat_tool_to_responses_tool(t) for t in (body.get("tools") or [])]

    # Map tool_choice
    tc_raw: str | dict[str, Any] | None = body.get("tool_choice")
    tc_resolved: str | dict[str, Any] = "auto"
    if isinstance(tc_raw, dict):
        tc_type = tc_raw.get("type", "auto")
        if tc_type == "function":
            tc_name = tc_raw.get("function", {}).get("name", "")
            tc_resolved = {"type": "function", "name": tc_name}
    elif tc_raw in ("none", "required"):
        tc_resolved = tc_raw
    # "auto" is the default — keep it

    return ResponsesCreateRequest(
        model=body.get("model", ""),
        input=messages,
        instructions=None,
        max_output_tokens=body.get("max_tokens"),
        temperature=body.get("temperature", 1.0),
        top_p=body.get("top_p", 1.0),
        stream=bool(body.get("stream", False)),
        tools=tools,
        tool_choice=tc_resolved,
        store=False,
        previous_response_id=None,
        metadata={},
        parallel_tool_calls=body.get("parallel_tool_calls", True),
        user=body.get("user"),
    )


# ---------------------------------------------------------------------------
# Response conversion  (Responses output -> chat.completions format)
# ---------------------------------------------------------------------------


def _extract_chat_response(responses_output: dict[str, Any]) -> dict[str, Any]:
    """Convert a Responses resource to a Chat Completions response."""
    response_id = responses_output.get("id", f"chatcmpl-{uuid.uuid4().hex[:12]}")
    created = responses_output.get("created_at", int(time.time()))
    model = responses_output.get("model", "")
    output = responses_output.get("output", [])
    usage = responses_output.get("usage")

    choices: list[dict[str, Any]] = []
    for idx, item in enumerate(output):
        item_type = item.get("type")
        if item_type == "function_call":
            message: dict[str, Any] = {"role": "assistant", "content": None}
            message["tool_calls"] = [
                {
                    "id": item.get("call_id", f"call_{uuid.uuid4().hex[:12]}"),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", "{}"),
                    },
                }
            ]
            choices.append(
                {
                    "index": idx,
                    "message": message,
                    "finish_reason": _map_finish_reason(
                        responses_output.get("status", "completed")
                    ),
                }
            )
            continue
        if item_type != "message":
            continue
        content = item.get("content", [])
        role = item.get("role", "assistant")

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in content:
            if block.get("type") in ("output_text", "text"):
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "function_call":
                tool_calls.append(
                    {
                        "id": block.get("call_id", f"call_{uuid.uuid4().hex[:12]}"),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": block.get("arguments", "{}"),
                        },
                    }
                )

        message = {"role": role}
        if text_parts:
            message["content"] = "\n".join(text_parts)
        if tool_calls:
            message["tool_calls"] = tool_calls

        choices.append(
            {
                "index": idx,
                "message": message,
                "finish_reason": _map_finish_reason(
                    responses_output.get("status", "completed")
                ),
            }
        )

    if not choices:
        choices = [
            {
                "index": 0,
                "message": {"role": "assistant", "content": None},
                "finish_reason": "stop",
            }
        ]

    result: dict[str, Any] = {
        "id": response_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": choices,
    }
    if usage:
        result["usage"] = usage
    return result


def _map_finish_reason(status: str) -> str:
    mapping = {
        "completed": "stop",
        "incomplete": "length",
        "failed": "error",
    }
    return mapping.get(status, "stop")


# ---------------------------------------------------------------------------
# Streaming: wrap Responses SSE -> Chat Completions SSE
# ---------------------------------------------------------------------------


async def _responses_sse_to_chat_stream(
    responses_stream: AsyncIterator[str],
) -> AsyncIterator[str]:
    """Wrap a Responses SSE stream into Chat Completions ``data:`` chunks."""
    response_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    model = ""
    text_buffer: dict[int, str] = {}
    tool_buffer: dict[int, list[dict[str, Any]]] = {}

    async for chunk in responses_stream:
        for line in chunk.splitlines(keepends=False):
            if line.startswith("data: "):
                try:
                    payload = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
            elif line.startswith("event: "):
                continue
            else:
                continue

            event_type = payload.get("type", "")
            if event_type == "response.created":
                model = payload.get("response", {}).get("model", "")
            elif event_type == "response.output_text.delta":
                response_data = payload.get("response", payload)
                idx = response_data.get("output_index", 0)
                delta = response_data.get("delta", "")
                text_buffer.setdefault(idx, "")
                text_buffer[idx] += delta
                yield (
                    json.dumps(
                        {
                            "id": response_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [
                                {
                                    "index": idx,
                                    "delta": {"content": delta},
                                    "finish_reason": None,
                                }
                            ],
                        }
                    )
                    + "\n\n"
                )
            elif event_type == "response.function_call_arguments.delta":
                response_data = payload.get("response", payload)
                idx = response_data.get("output_index", 0)
                delta = response_data.get("delta", "")
                if idx not in tool_buffer:
                    tool_buffer[idx] = []
                yield (
                    json.dumps(
                        {
                            "id": response_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [
                                {
                                    "index": idx,
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": 0,
                                                "function": {"arguments": delta},
                                            }
                                        ]
                                    },
                                    "finish_reason": None,
                                }
                            ],
                        }
                    )
                    + "\n\n"
                )
            elif event_type in ("response.completed", "response.incomplete"):
                yield (
                    json.dumps(
                        {
                            "id": response_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {},
                                    "finish_reason": _map_finish_reason(
                                        "completed"
                                        if event_type == "response.completed"
                                        else "incomplete"
                                    ),
                                }
                            ],
                        }
                    )
                    + "\n\n"
                )
                yield "data: [DONE]\n\n"
