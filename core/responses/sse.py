"""SSE builders and an Anthropic-to-Responses adapter for ``codexproxy``.

Two responsibilities live here:

* :class:`ResponsesSSEBuilder` formats individual Responses events.
* :class:`AnthropicToResponsesAdapter` consumes the existing
  ``Anthropic-format`` SSE strings produced by every provider in
  ``codexproxy`` and re-emits them as Responses-shaped SSE.

The adapter re-uses the upstream text/tool streams without touching the
provider implementations, so a single Responses service layer can drive
all 17 providers with zero per-provider Responses plumbing in v0.1.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

# Standard headers for Responses-style ``text/event-stream`` responses.
RESPONSES_SSE_RESPONSE_HEADERS: dict[str, str] = {
    "X-Accel-Buffering": "no",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
}


def format_responses_sse(event_type: str, data: dict[str, Any]) -> str:
    """Format one Responses-style SSE event."""
    return f"event: {event_type}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def new_response_id() -> str:
    """Generate a new Responses-style response id (``resp_<uuid>``)."""
    return f"resp_{uuid.uuid4().hex}"


def new_output_item_id() -> str:
    """Generate a new output item id (``msg_<uuid>`` or ``fc_<uuid>`` are
    acceptable; we use ``msg_`` for messages and ``fc_`` for function
    calls below)."""
    return f"msg_{uuid.uuid4().hex}"


def new_function_call_id() -> str:
    return f"fc_{uuid.uuid4().hex}"


def new_reasoning_id() -> str:
    """Generate a new reasoning item id (``rs_<uuid>``)."""
    return f"rs_{uuid.uuid4().hex}"


def new_call_id() -> str:
    """Call id used by ``function_call_output`` to link back to a call."""
    return f"call_{uuid.uuid4().hex}"


# ---------------------------------------------------------------------------
# Argument translation for function calls
# ---------------------------------------------------------------------------


def _translate_tool_arguments(tool_name: str, arguments_json: str) -> str:
    """Translate model-friendly function call arguments to Codex CLI format.

    Non-OpenAI providers see simplified tool schemas and generate
    arguments in those terms.  Before they reach Codex CLI they must be
    rewritten to the format that Codex CLI's handlers expect.
    """
    if tool_name != "apply_patch":
        return arguments_json

    try:
        args = json.loads(arguments_json)
    except json.JSONDecodeError:
        return arguments_json

    # Model called apply_patch with {"patch": "..."}.
    # Codex CLI expects {"cmd": ["apply_patch", "..."]}.
    if "patch" in args and isinstance(args["patch"], str):
        args["cmd"] = ["apply_patch", args["patch"]]
        del args["patch"]
        return json.dumps(args, separators=(",", ":"))

    return arguments_json


# ---------------------------------------------------------------------------
# Event builders
# ---------------------------------------------------------------------------


def build_response_created(
    *,
    response_id: str,
    created_at: int,
    model: str,
    instructions: str | None,
    metadata: dict[str, Any],
    tools: list[dict[str, Any]],
    tool_choice: Any,
    temperature: float,
    top_p: float,
    parallel_tool_calls: bool,
    previous_response_id: str | None,
    store: bool,
    user: str | None,
) -> str:
    payload = {
        "type": "response.created",
        "response": {
            "id": response_id,
            "object": "response",
            "created_at": created_at,
            "status": "in_progress",
            "background": False,
            "error": None,
            "incomplete_details": None,
            "instructions": instructions,
            "metadata": metadata,
            "model": model,
            "output": [],
            "parallel_tool_calls": parallel_tool_calls,
            "previous_response_id": previous_response_id,
            "reasoning": None,
            "store": store,
            "temperature": temperature,
            "text": None,
            "tool_choice": tool_choice,
            "tools": tools,
            "top_p": top_p,
            "truncation": "disabled",
            "usage": {
                "input_tokens": 0,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 0,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 0,
            },
            "user": user,
        },
    }
    return format_responses_sse("response.created", payload)


def build_response_in_progress(response_id: str) -> str:
    return format_responses_sse(
        "response.in_progress",
        {
            "type": "response.in_progress",
            "response": {"id": response_id, "status": "in_progress"},
        },
    )


def build_output_item_added(item: dict[str, Any], output_index: int) -> str:
    return format_responses_sse(
        "response.output_item.added",
        {
            "type": "response.output_item.added",
            "output_index": output_index,
            "item": item,
        },
    )


def build_content_part_added(
    *,
    item_id: str,
    output_index: int,
    content_index: int,
    part: dict[str, Any],
) -> str:
    return format_responses_sse(
        "response.content_part.added",
        {
            "type": "response.content_part.added",
            "item_id": item_id,
            "output_index": output_index,
            "content_index": content_index,
            "part": part,
        },
    )


def build_output_text_delta(
    *,
    item_id: str,
    output_index: int,
    content_index: int,
    delta: str,
    logprobs: list[Any] | None = None,
) -> str:
    return format_responses_sse(
        "response.output_text.delta",
        {
            "type": "response.output_text.delta",
            "item_id": item_id,
            "output_index": output_index,
            "content_index": content_index,
            "delta": delta,
            "logprobs": logprobs or [],
        },
    )


def build_output_text_done(
    *,
    item_id: str,
    output_index: int,
    content_index: int,
    text: str,
) -> str:
    return format_responses_sse(
        "response.output_text.done",
        {
            "type": "response.output_text.done",
            "item_id": item_id,
            "output_index": output_index,
            "content_index": content_index,
            "text": text,
        },
    )


def build_content_part_done(
    *,
    item_id: str,
    output_index: int,
    content_index: int,
    part: dict[str, Any],
) -> str:
    return format_responses_sse(
        "response.content_part.done",
        {
            "type": "response.content_part.done",
            "item_id": item_id,
            "output_index": output_index,
            "content_index": content_index,
            "part": part,
        },
    )


def build_function_call_arguments_delta(
    *,
    item_id: str,
    output_index: int,
    delta: str,
) -> str:
    return format_responses_sse(
        "response.function_call_arguments.delta",
        {
            "type": "response.function_call_arguments.delta",
            "item_id": item_id,
            "output_index": output_index,
            "delta": delta,
        },
    )


def build_reasoning_summary_delta(
    *,
    item_id: str,
    delta: str,
) -> str:
    return format_responses_sse(
        "response.reasoning_summary.delta",
        {
            "type": "response.reasoning_summary.delta",
            "item_id": item_id,
            "delta": delta,
        },
    )


def build_reasoning_summary_done(
    *,
    item_id: str,
    summary: list[dict[str, Any]],
) -> str:
    return format_responses_sse(
        "response.reasoning_summary.done",
        {
            "type": "response.reasoning_summary.done",
            "item_id": item_id,
            "summary": summary,
        },
    )


def build_function_call_arguments_done(
    *,
    item_id: str,
    output_index: int,
    arguments: str,
) -> str:
    return format_responses_sse(
        "response.function_call_arguments.done",
        {
            "type": "response.function_call_arguments.done",
            "item_id": item_id,
            "output_index": output_index,
            "arguments": arguments,
        },
    )


def build_output_item_done(item: dict[str, Any], output_index: int) -> str:
    return format_responses_sse(
        "response.output_item.done",
        {
            "type": "response.output_item.done",
            "output_index": output_index,
            "item": item,
        },
    )


def build_response_completed(
    *,
    response_id: str,
    created_at: int,
    completed_at: int,
    model: str,
    output: list[dict[str, Any]],
    usage: dict[str, Any],
    instructions: str | None,
    metadata: dict[str, Any],
    tools: list[dict[str, Any]],
    tool_choice: Any,
    temperature: float,
    top_p: float,
    parallel_tool_calls: bool,
    previous_response_id: str | None,
    store: bool,
    user: str | None,
    status: str = "completed",
    error: dict[str, Any] | None = None,
) -> str:
    payload = {
        "type": "response.completed"
        if status == "completed"
        else "response.incomplete",
        "response": {
            "id": response_id,
            "object": "response",
            "created_at": created_at,
            "completed_at": completed_at,
            "status": status,
            "background": False,
            "error": error,
            "incomplete_details": None,
            "instructions": instructions,
            "metadata": metadata,
            "model": model,
            "output": output,
            "parallel_tool_calls": parallel_tool_calls,
            "previous_response_id": previous_response_id,
            "reasoning": None,
            "store": store,
            "temperature": temperature,
            "text": None,
            "tool_choice": tool_choice,
            "tools": tools,
            "top_p": top_p,
            "truncation": "disabled",
            "usage": usage,
            "user": user,
        },
    }
    return format_responses_sse(payload["type"], payload)


# ---------------------------------------------------------------------------
# Anthropic-format SSE parser + Responses adapter
# ---------------------------------------------------------------------------


def _split_sse_events(buffer: str) -> tuple[list[tuple[str, dict[str, Any]]], str]:
    """Split a chunk of Anthropic-style SSE into ``(event_type, data)`` pairs.

    Handles multiple ``data:`` lines per event (concatenated with ``\\n`` per
    SSE spec) and preserves ``\\n\\n`` inside data values by using a line-based
    state machine instead of a naive ``\\n\\n`` split.

    Returns the events that completed within ``buffer`` and the trailing
    incomplete event text that should be re-fed on the next call.
    """
    events: list[tuple[str, dict[str, Any]]] = []
    event_type: str | None = None
    data_lines: list[str] = []

    for line in buffer.splitlines(keepends=False):
        if not line.strip():
            # Empty line — event boundary. Finalize the pending event.
            if data_lines:
                data_raw = "\n".join(data_lines)
                try:
                    data = json.loads(data_raw)
                except json.JSONDecodeError:
                    pass
                else:
                    events.append((event_type or "", data))
            event_type = None
            data_lines = []
            continue

        if line.startswith("event:"):
            event_type = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].strip())

    # Whatever is left is an incomplete event.
    remaining_lines: list[str] = []
    if event_type is not None:
        remaining_lines.append(f"event: {event_type}")
    remaining_lines.extend(f"data: {dl}" for dl in data_lines)
    remaining = "\n".join(remaining_lines)
    if remaining:
        remaining += "\n"
    return events, remaining


@dataclass
class _PendingMessageState:
    """Tracks a single Responses output message being assembled."""

    item_id: str
    output_index: int
    text: str = ""
    started: bool = False
    part_added: bool = False
    part: dict[str, Any] = field(default_factory=dict)


@dataclass
class _PendingReasoningState:
    """Tracks a single Responses reasoning output item being assembled."""

    item_id: str
    output_index: int
    text: str = ""
    started: bool = False


@dataclass
class _PendingFunctionCallState:
    """Tracks a single Responses function call being assembled."""

    item_id: str
    call_id: str
    name: str
    output_index: int
    arguments: str = ""
    started: bool = False


class AnthropicToResponsesAdapter:
    """Translate Anthropic-style SSE strings into Responses-shaped SSE.

    The adapter is stateful across ``feed()`` calls so it can be driven
    by an :class:`AsyncIterator` of provider chunks without buffering the
    full stream in memory.
    """

    def __init__(
        self,
        *,
        response_id: str,
        created_at: int,
        model: str,
        instructions: str | None,
        metadata: dict[str, Any] | None,
        tools: list[dict[str, Any]],
        tool_choice: Any,
        temperature: float,
        top_p: float,
        parallel_tool_calls: bool,
        previous_response_id: str | None,
        store: bool,
        user: str | None,
    ) -> None:
        self._response_id = response_id
        self._created_at = created_at
        self._model = model
        self._instructions = instructions
        self._metadata = metadata or {}
        self._tools = tools
        self._tool_choice = tool_choice
        self._temperature = temperature
        self._top_p = top_p
        self._parallel_tool_calls = parallel_tool_calls
        self._previous_response_id = previous_response_id
        self._store = store
        self._user = user
        self._buffer = ""
        self._open_message: _PendingMessageState | None = None
        self._open_function_call: _PendingFunctionCallState | None = None
        self._open_reasoning: _PendingReasoningState | None = None
        self._closed_messages: list[dict[str, Any]] = []
        self._closed_function_calls: list[dict[str, Any]] = []
        self._closed_reasoning: list[dict[str, Any]] = []
        self._completed = False
        self._completed_emitted = False
        self._usage_input_tokens = 0
        self._usage_output_tokens = 0
        self._error: dict[str, Any] | None = None
        self._status = "completed"
        self._output_index = 0

    # ------------------------------------------------------------------
    # Stream construction
    # ------------------------------------------------------------------
    def opening_events(self) -> Iterator[str]:
        """Yield the fixed opening events (created + in_progress)."""
        yield build_response_created(
            response_id=self._response_id,
            created_at=self._created_at,
            model=self._model,
            instructions=self._instructions,
            metadata=self._metadata,
            tools=self._tools,
            tool_choice=self._tool_choice,
            temperature=self._temperature,
            top_p=self._top_p,
            parallel_tool_calls=self._parallel_tool_calls,
            previous_response_id=self._previous_response_id,
            store=self._store,
            user=self._user,
        )
        yield build_response_in_progress(self._response_id)

    def feed(self, chunk: str) -> Iterator[str]:
        """Consume one Anthropic SSE chunk and yield Responses SSE events."""
        if self._completed:
            return
        self._buffer += chunk
        events, self._buffer = _split_sse_events(self._buffer)
        for event_type, data in events:
            yield from self._handle_event(event_type, data)

    def finalize(self) -> Iterator[str]:
        """Yield any closing events that the upstream never sent.

        Providers occasionally drop the terminal events on error; this
        method flushes whatever the adapter is currently holding and
        emits ``response.completed`` (or ``response.incomplete`` if an
        error was recorded).
        """
        if self._open_message is not None:
            yield from self._close_open_message()
        if self._open_function_call is not None:
            yield from self._close_open_function_call()
        if self._open_reasoning is not None:
            yield from self._close_open_reasoning()
        yield from self._emit_completed()

    # ------------------------------------------------------------------
    # Accessors used by the service layer to materialise the full response
    # ------------------------------------------------------------------
    @property
    def response_id(self) -> str:
        return self._response_id

    @property
    def output(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        items.extend(self._closed_messages)
        items.extend(self._closed_function_calls)
        items.extend(self._closed_reasoning)
        return items

    @property
    def usage(self) -> dict[str, Any]:
        return {
            "input_tokens": self._usage_input_tokens,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": self._usage_output_tokens,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": self._usage_input_tokens + self._usage_output_tokens,
        }

    @property
    def status(self) -> str:
        return self._status

    # ------------------------------------------------------------------
    # Internal event handling
    # ------------------------------------------------------------------
    def _handle_event(self, event_type: str, data: dict[str, Any]) -> Iterator[str]:
        if event_type == "message_start":
            message = data.get("message", {})
            usage = message.get("usage", {})
            self._usage_input_tokens = int(usage.get("input_tokens", 0) or 0)
            self._usage_output_tokens = int(usage.get("output_tokens", 0) or 0)
            return
        if event_type == "content_block_start":
            block = data.get("content_block", {})
            block_type = block.get("type")
            if block_type == "text":
                yield from self._open_text_block(data, block)
            elif block_type == "tool_use":
                yield from self._open_tool_use_block(data, block)
            elif block_type == "thinking":
                yield from self._open_reasoning_block(data, block)
            return
        if event_type == "content_block_delta":
            delta = data.get("delta", {})
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                yield from self._apply_text_delta(delta)
            elif delta_type == "input_json_delta":
                yield from self._apply_tool_input_delta(delta)
            elif delta_type == "thinking_delta":
                yield from self._apply_reasoning_delta(delta)
            return
        if event_type == "content_block_stop":
            yield from self._handle_block_stop()
            return
        if event_type == "message_delta":
            usage = data.get("usage", {})
            output_tokens = int(usage.get("output_tokens", 0) or 0)
            if output_tokens:
                self._usage_output_tokens = output_tokens
            return
        if event_type == "message_stop":
            self._completed = True
            return
        if event_type == "error":
            self._error = data
            self._status = "failed"
            return
        # ``ping`` and other unknown event types are intentionally ignored.

    # ------------------------------------------------------------------
    # Block lifecycle
    # ------------------------------------------------------------------
    def _open_text_block(
        self, data: dict[str, Any], block: dict[str, Any]
    ) -> Iterator[str]:
        if self._open_message is not None:
            yield from self._close_open_message()
        if self._open_function_call is not None:
            yield from self._close_open_function_call()
        if self._open_reasoning is not None:
            yield from self._close_open_reasoning()
        item_id = new_output_item_id()
        message_item = {
            "id": item_id,
            "type": "message",
            "role": "assistant",
            "status": "in_progress",
            "content": [],
        }
        part = {
            "type": "output_text",
            "text": "",
            "annotations": [],
        }
        self._open_message = _PendingMessageState(
            item_id=item_id,
            output_index=self._output_index,
            started=True,
            part_added=True,
            part=part,
        )
        self._output_index += 1
        yield build_output_item_added(message_item, self._open_message.output_index)
        yield build_content_part_added(
            item_id=item_id,
            output_index=self._open_message.output_index,
            content_index=0,
            part=part,
        )

    def _apply_text_delta(self, delta: dict[str, Any]) -> Iterator[str]:
        if self._open_message is None:
            return
        text = delta.get("text", "")
        if not text:
            return
        self._open_message.text += text
        yield build_output_text_delta(
            item_id=self._open_message.item_id,
            output_index=self._open_message.output_index,
            content_index=0,
            delta=text,
        )

    def _open_tool_use_block(
        self, data: dict[str, Any], block: dict[str, Any]
    ) -> Iterator[str]:
        if self._open_message is not None:
            yield from self._close_open_message()
        if self._open_function_call is not None:
            yield from self._close_open_function_call()
        if self._open_reasoning is not None:
            yield from self._close_open_reasoning()
        item_id = block.get("id") or new_function_call_id()
        call_id = item_id
        name = block.get("name", "")
        item = {
            "id": item_id,
            "type": "function_call",
            "call_id": call_id,
            "name": name,
            "arguments": "",
            "status": "in_progress",
        }
        self._open_function_call = _PendingFunctionCallState(
            item_id=item_id,
            call_id=call_id,
            name=name,
            output_index=self._output_index,
            started=True,
        )
        self._output_index += 1
        yield build_output_item_added(item, self._open_function_call.output_index)

    def _open_reasoning_block(
        self, data: dict[str, Any], block: dict[str, Any]
    ) -> Iterator[str]:
        if self._open_message is not None:
            yield from self._close_open_message()
        if self._open_function_call is not None:
            yield from self._close_open_function_call()
        if self._open_reasoning is not None:
            yield from self._close_open_reasoning()
        item_id = new_reasoning_id()
        item = {
            "id": item_id,
            "type": "reasoning",
            "status": "in_progress",
            "summary": [],
        }
        self._open_reasoning = _PendingReasoningState(
            item_id=item_id,
            output_index=self._output_index,
            started=True,
        )
        self._output_index += 1
        yield build_output_item_added(item, self._open_reasoning.output_index)

    def _apply_reasoning_delta(self, delta: dict[str, Any]) -> Iterator[str]:
        if self._open_reasoning is None:
            return
        text = delta.get("thinking", "") or delta.get("text", "")
        if not text:
            return
        self._open_reasoning.text += text
        yield build_reasoning_summary_delta(
            item_id=self._open_reasoning.item_id,
            delta=text,
        )

    def _close_open_reasoning(self) -> Iterator[str]:
        reasoning = self._open_reasoning
        assert reasoning is not None
        summary = [{"type": "summary_text", "text": reasoning.text}]
        item = {
            "id": reasoning.item_id,
            "type": "reasoning",
            "status": "completed",
            "summary": summary,
        }
        yield build_reasoning_summary_done(
            item_id=reasoning.item_id,
            summary=summary,
        )
        yield build_output_item_done(item, reasoning.output_index)
        self._closed_reasoning.append(item)
        self._open_reasoning = None

    def _apply_tool_input_delta(self, delta: dict[str, Any]) -> Iterator[str]:
        if self._open_function_call is None:
            return
        partial = delta.get("partial_json", "")
        if not partial:
            return
        self._open_function_call.arguments += partial
        yield build_function_call_arguments_delta(
            item_id=self._open_function_call.item_id,
            output_index=self._open_function_call.output_index,
            delta=partial,
        )

    def _handle_block_stop(self) -> Iterator[str]:
        if self._open_function_call is not None:
            yield from self._close_open_function_call()
        elif self._open_message is not None:
            yield from self._close_open_message()
        elif self._open_reasoning is not None:
            yield from self._close_open_reasoning()
        return

    def _close_open_message(self) -> Iterator[str]:
        msg = self._open_message
        assert msg is not None
        part = dict(msg.part)
        part["text"] = msg.text
        item = {
            "id": msg.item_id,
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [part],
        }
        yield build_output_text_done(
            item_id=msg.item_id,
            output_index=msg.output_index,
            content_index=0,
            text=msg.text,
        )
        yield build_content_part_done(
            item_id=msg.item_id,
            output_index=msg.output_index,
            content_index=0,
            part=part,
        )
        yield build_output_item_done(item, msg.output_index)
        self._closed_messages.append(item)
        self._open_message = None

    def _close_open_function_call(self) -> Iterator[str]:
        call = self._open_function_call
        assert call is not None
        # Translate apply_patch arguments from model-friendly format
        # {"patch": "..."} to Codex CLI format {"cmd": ["apply_patch", "..."]}.
        arguments = _translate_tool_arguments(call.name, call.arguments)
        item = {
            "id": call.item_id,
            "type": "function_call",
            "call_id": call.call_id,
            "name": call.name,
            "arguments": arguments,
            "status": "completed",
        }
        yield build_function_call_arguments_done(
            item_id=call.item_id,
            output_index=call.output_index,
            arguments=arguments,
        )
        yield build_output_item_done(item, call.output_index)
        self._closed_function_calls.append(item)
        self._open_function_call = None

    def _emit_completed(self) -> Iterator[str]:
        if self._completed_emitted:
            return
        self._completed_emitted = True
        self._completed = True
        yield build_response_completed(
            response_id=self._response_id,
            created_at=self._created_at,
            completed_at=int(time.time()),
            model=self._model,
            output=self.output,
            usage=self.usage,
            instructions=self._instructions,
            metadata=self._metadata,
            tools=self._tools,
            tool_choice=self._tool_choice,
            temperature=self._temperature,
            top_p=self._top_p,
            parallel_tool_calls=self._parallel_tool_calls,
            previous_response_id=self._previous_response_id,
            store=self._store,
            user=self._user,
            status=self._status,
            error=self._error,
        )
