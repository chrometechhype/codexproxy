"""Service layer for the OpenAI Responses API endpoints.

The service hides the Anthropic wire surface from the Responses routes.
It re-uses the existing provider transports (every provider already
streams Anthropic-format SSE) by routing their output through
:class:`core.responses.sse.AnthropicToResponsesAdapter`.
"""

from __future__ import annotations

import json
import time
import traceback
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger
from pydantic import TypeAdapter

from config.provider_ids import SUPPORTED_PROVIDER_IDS
from config.settings import Settings
from core.responses.sse import (
    AnthropicToResponsesAdapter,
    new_response_id,
)
from core.responses.store import ResponseStore, StoredResponse
from providers.base import BaseProvider

from .model_router import ModelRouter
from .models.anthropic import (
    ContentBlockDocument,
    ContentBlockImage,
    ContentBlockRedactedThinking,
    ContentBlockServerToolUse,
    ContentBlockText,
    ContentBlockThinking,
    ContentBlockToolResult,
    ContentBlockToolUse,
    ContentBlockWebFetchToolResult,
    ContentBlockWebSearchToolResult,
    Message,
    MessagesRequest,
    SystemContent,
    ThinkingConfig,
    Tool,
)
from .models.responses import (
    ResponsesCreateRequest,
    ResponsesInputItemsList,
    ResponsesInputItemView,
    ResponsesInputMessage,
)

ProviderGetter = Callable[[str], BaseProvider]


@dataclass(frozen=True, slots=True)
class _Resolved:
    provider_id: str
    provider_model: str
    thinking_enabled: bool


class ResponsesService:
    """Bridge the Responses API to the existing provider transport stack."""

    def __init__(
        self,
        settings: Settings,
        *,
        provider_getter: ProviderGetter,
        store: ResponseStore | None = None,
    ) -> None:
        self._settings = settings
        self._provider_getter = provider_getter
        self._store = store or ResponseStore()

    @property
    def store(self) -> ResponseStore:
        return self._store

    # ------------------------------------------------------------------
    # Streaming entry point used by ``POST /v1/responses``
    # ------------------------------------------------------------------
    async def stream_create(
        self, request: ResponsesCreateRequest
    ) -> AsyncIterator[str]:
        """Yield Responses SSE events for a streaming ``POST /v1/responses``."""
        messages_request, resolved = self._build_messages_request(request)
        provider = self._provider_getter(resolved.provider_id)
        response_id = new_response_id()
        created_at = int(time.time())
        tools_payload = _expand_namespace_tools(request.tools or [])
        tool_choice_payload = _tool_choice_to_payload(request.tool_choice)
        adapter = AnthropicToResponsesAdapter(
            response_id=response_id,
            created_at=created_at,
            model=request.model,
            instructions=request.instructions,
            metadata=request.metadata or {},
            tools=tools_payload,
            tool_choice=tool_choice_payload,
            temperature=float(request.temperature or 1.0),
            top_p=float(request.top_p or 1.0),
            parallel_tool_calls=bool(request.parallel_tool_calls),
            previous_response_id=request.previous_response_id,
            store=bool(request.store),
            user=request.user,
        )
        for event in adapter.opening_events():
            yield event

        try:
            async for chunk in provider.stream_response(
                messages_request,
                input_tokens=0,
                request_id=response_id,
                thinking_enabled=resolved.thinking_enabled,
            ):
                for event in adapter.feed(chunk):
                    yield event
        except Exception as exc:
            settings = self._settings
            if settings.log_api_error_tracebacks:
                logger.error(
                    "Responses stream error request_id={}: {}", response_id, exc
                )
                logger.error(traceback.format_exc())
            else:
                logger.error(
                    "Responses stream error request_id={} exc_type={}",
                    response_id,
                    type(exc).__name__,
                )
            for event in adapter.feed(
                'event: error\ndata: {"type":"error","error":{"type":"api_error",'
                f'"message":"{type(exc).__name__}"}}\n\n'
            ):
                yield event

        for event in adapter.finalize():
            yield event

        self._store.put(
            StoredResponse(
                id=response_id,
                created_at=created_at,
                completed_at=created_at,
                model=request.model,
                status=adapter.status,
                output=adapter.output,
                usage=adapter.usage,
                error=None,
                instructions=request.instructions,
                metadata=request.metadata or {},
                tools=tools_payload,
                tool_choice=tool_choice_payload,
                temperature=float(request.temperature or 1.0),
                top_p=float(request.top_p or 1.0),
                parallel_tool_calls=bool(request.parallel_tool_calls),
                previous_response_id=request.previous_response_id,
                store=bool(request.store),
                user=request.user,
                input_items=_input_items_to_storage(request),
            )
        )

    # ------------------------------------------------------------------
    # Non-streaming entry point (collects the SSE stream into JSON).
    # ------------------------------------------------------------------
    async def create(self, request: ResponsesCreateRequest) -> dict[str, Any]:
        """Aggregate the streaming output into a single Responses resource."""
        chunks = [chunk async for chunk in self.stream_create(request)]
        return _sse_chunks_to_response(chunks)

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------
    def get_response(self, response_id: str) -> dict[str, Any] | None:
        stored = self._store.get(response_id)
        if stored is None:
            return None
        return _stored_to_response(stored)

    def get_input_items(self, response_id: str) -> ResponsesInputItemsList | None:
        stored = self._store.get(response_id)
        if stored is None:
            return None
        views: list[ResponsesInputItemView] = []
        for idx, item in enumerate(stored.input_items):
            view = _input_item_to_view(idx, item)
            if view is not None:
                views.append(view)
        return ResponsesInputItemsList(
            data=views,
            first_id=(views[0].type if views else None),
            last_id=(views[-1].type if views else None),
            has_more=False,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _build_messages_request(
        self, request: ResponsesCreateRequest
    ) -> tuple[MessagesRequest, _Resolved]:
        messages, system = _responses_input_to_messages(request)
        provider_model_ref = self._resolve_provider_model_ref(request.model)
        provider_id = _parse_provider_id(provider_model_ref)
        provider_model = _strip_provider_prefix(provider_model_ref, provider_id)
        max_tokens = request.max_output_tokens or 4096
        thinking_enabled = self._settings.resolve_thinking(request.model)
        anthropic_tools = [
            _payload_to_tool(t) for t in _expand_namespace_tools(request.tools or [])
        ]
        anthropic_tools = [t for t in anthropic_tools if t is not None]
        return (
            MessagesRequest(
                model=provider_model,
                max_tokens=max_tokens,
                messages=messages,
                system=system,
                stream=True,
                temperature=request.temperature,
                top_p=request.top_p,
                tools=anthropic_tools or None,
                tool_choice=_payload_to_tool_choice(request.tool_choice),
                thinking=ThinkingConfig(enabled=thinking_enabled),
            ),
            _Resolved(
                provider_id=provider_id,
                provider_model=provider_model,
                thinking_enabled=thinking_enabled,
            ),
        )

    def _resolve_provider_model_ref(self, model: str) -> str:
        """Pick the provider/model string for the requested Codex model.

        If the model already encodes a provider prefix (``provider/model``),
        use it directly. Otherwise reuse the configured default ``MODEL``.
        """
        if "/" in model and model.split("/", 1)[0] in SUPPORTED_PROVIDER_IDS:
            return model
        router = ModelRouter(self._settings)
        resolved = router.resolve(model)
        return resolved.provider_model_ref


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_provider_id(model_ref: str) -> str:
    if "/" in model_ref and model_ref.split("/", 1)[0] in SUPPORTED_PROVIDER_IDS:
        return model_ref.split("/", 1)[0]
    return Settings.parse_provider_type(model_ref)


def _strip_provider_prefix(model_ref: str, provider_id: str) -> str:
    prefix = f"{provider_id}/"
    if model_ref.startswith(prefix):
        return model_ref[len(prefix) :]
    return Settings.parse_model_name(model_ref)


def _responses_input_to_messages(
    request: ResponsesCreateRequest,
) -> tuple[list[Message], str | list[SystemContent] | None]:
    """Translate the Responses ``input`` field into Anthropic messages."""
    items = _normalise_input_items(request.input, request.instructions)
    messages: list[Message] = []
    system_parts: list[str] = []
    for item in items:
        if item["type"] == "message":
            role = item.get("role", "user")
            content = item.get("content", "")
            if role == "system" or role == "developer":
                if isinstance(content, str):
                    system_parts.append(content)
                else:
                    translated = _responses_content_to_anthropic(content)
                    if isinstance(translated, str):
                        if translated:
                            system_parts.append(translated)
                    else:
                        system_parts.extend(
                            block.text
                            for block in translated
                            if isinstance(block, ContentBlockText) and block.text
                        )
                continue
            translated_content = _responses_content_to_anthropic(content)
            messages.append(
                Message(
                    role=role if role in ("user", "assistant") else "user",
                    content=translated_content,
                )
            )
        elif item["type"] == "function_call":
            arguments = item.get("arguments", "")
            try:
                parsed = json.loads(arguments) if arguments else {}
            except json.JSONDecodeError:
                parsed = {}
            messages.append(
                Message(
                    role="assistant",
                    content=[
                        ContentBlockToolUse(
                            type="tool_use",
                            id=item.get("call_id") or item.get("id", ""),
                            name=item.get("name", ""),
                            input=parsed,
                        )
                    ],
                )
            )
        elif item["type"] == "function_call_output":
            messages.append(
                Message(
                    role="user",
                    content=[
                        ContentBlockToolResult(
                            type="tool_result",
                            tool_use_id=item.get("call_id", ""),
                            content=item.get("output", ""),
                        )
                    ],
                )
            )
    if request.instructions and not system_parts:
        system_parts.append(request.instructions)
    system: str | list[SystemContent] | None = "\n\n".join(system_parts) or None
    if not messages:
        messages.append(Message(role="user", content=""))
    return messages, system


_CONTENT_BLOCK_UNION = (
    ContentBlockText
    | ContentBlockImage
    | ContentBlockDocument
    | ContentBlockToolUse
    | ContentBlockToolResult
    | ContentBlockThinking
    | ContentBlockRedactedThinking
    | ContentBlockServerToolUse
    | ContentBlockWebSearchToolResult
    | ContentBlockWebFetchToolResult
)

_CONTENT_BLOCK_ADAPTER: TypeAdapter[Any] = TypeAdapter(_CONTENT_BLOCK_UNION)
_CONTENT_LIST_ADAPTER: TypeAdapter[Any] = TypeAdapter(list[_CONTENT_BLOCK_UNION])


def _responses_content_to_anthropic(
    content: str | list[Any] | None,
) -> str | list[_CONTENT_BLOCK_UNION]:
    """Translate a Responses ``content`` value into Anthropic content blocks.

    The Codex CLI ships Responses-style content parts such as
    ``{"type": "input_text", "text": "..."}`` and
    ``{"type": "input_image", ...}``. The Anthropic Messages Pydantic model
    only accepts the legacy ``text`` / ``image`` types, so we rewrite the
    well-known Responses shapes here. Unknown blocks are validated against
    the Anthropic content union; parts that still don't match are skipped.
    """

    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    translated: list[Any] = []
    for part in content:
        if isinstance(part, str):
            translated.append(ContentBlockText(type="text", text=part))
            continue
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type in ("input_text", "text"):
            translated.append(
                ContentBlockText(
                    type="text",
                    text=part.get("text", ""),
                )
            )
        elif part_type in ("input_image", "image"):
            image_dict: dict[str, Any] = {"type": "image", "source": {}}
            for key in ("source", "media_type", "detail"):
                if key in part:
                    image_dict[key] = part[key]
            try:
                translated.append(_CONTENT_BLOCK_ADAPTER.validate_python(image_dict))
            except Exception:
                continue
        else:
            try:
                translated.append(_CONTENT_BLOCK_ADAPTER.validate_python(part))
            except Exception:
                continue
    if not translated:
        return ""
    if len(translated) == 1 and isinstance(translated[0], ContentBlockText):
        return translated[0].text
    return _CONTENT_LIST_ADAPTER.validate_python(translated)


def _normalise_input_items(
    input_value: Any, instructions: str | None
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if input_value is None:
        if instructions:
            items.append({"type": "message", "role": "system", "content": instructions})
        return items
    if isinstance(input_value, str):
        items.append({"type": "message", "role": "user", "content": input_value})
        return items
    if not isinstance(input_value, list):
        return items
    for raw in input_value:
        if isinstance(raw, str):
            items.append({"type": "message", "role": "user", "content": raw})
            continue
        if isinstance(raw, ResponsesInputMessage):
            items.append(raw.model_dump())
            continue
        if isinstance(raw, dict):
            items.append(dict(raw))
            continue
        items.append(raw.model_dump())
    return items


def _input_items_to_storage(
    request: ResponsesCreateRequest,
) -> list[dict[str, Any]]:
    """Snapshot the request's input items for ``GET /v1/responses/{id}/input_items``."""
    items = _normalise_input_items(request.input, request.instructions)
    return items


def _input_item_to_view(
    idx: int, item: dict[str, Any]
) -> ResponsesInputItemView | None:
    item_type = item.get("type")
    if item_type == "message":
        return ResponsesInputItemView(
            type="message",
            role=item.get("role", "user"),
            content=item.get("content", ""),
            id=item.get("id") or f"msg_{idx}",
        )
    if item_type == "function_call":
        return ResponsesInputItemView(
            type="function_call",
            id=item.get("id") or f"fc_{idx}",
            call_id=item.get("call_id"),
            name=item.get("name"),
            arguments=item.get("arguments"),
        )
    if item_type == "function_call_output":
        return ResponsesInputItemView(
            type="function_call_output",
            call_id=item.get("call_id"),
            output=item.get("output"),
        )
    return None


def _expand_namespace_tools(tools: list[Any]) -> list[dict[str, Any]]:
    """Expand namespace-type tools into plain ``function`` tools.

    Codex CLI wraps MCP and other tool groups inside
    ``{"type": "namespace", "name": "mcp__<server>", "tools": [...]}``
    which is a non-standard Responses API extension. OpenAI and Azure
    expand these natively, but other backends must flatten them.

    Each inner tool gets a deterministic prefixed name so Codex CLI can
    route ``function_call`` responses back to the correct MCP server::

        mcp__<server>__<tool_name>

    Namespace tools that contain no inner tools are dropped; plain
    ``function`` tools pass through unchanged.
    """
    expanded: list[dict[str, Any]] = []
    for tool in tools:
        raw = _tool_to_payload(tool)
        if raw.get("type") == "namespace":
            namespace_name = raw.get("name", "")
            inner_tools = raw.get("tools", [])
            if not isinstance(inner_tools, list):
                continue
            for inner in inner_tools:
                inner_raw = _tool_to_payload(inner)
                if inner_raw.get("type") not in (None, "function"):
                    continue
                if "name" not in inner_raw:
                    continue
                prefixed = dict(inner_raw)
                prefixed["name"] = f"{namespace_name}__{inner_raw['name']}"
                expanded.append(prefixed)
        else:
            expanded.append(raw)
    return expanded


def _tool_to_payload(tool: Any) -> dict[str, Any]:
    if isinstance(tool, dict):
        return dict(tool)
    if hasattr(tool, "model_dump"):
        return tool.model_dump()
    return {}


def _tool_choice_to_payload(choice: Any) -> Any:
    if isinstance(choice, dict):
        return dict(choice)
    return choice


def _payload_to_tool(payload: Any) -> Tool | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("type") not in (None, "function"):
        return None
    if "name" not in payload:
        return None
    parameters = payload.get("parameters") or {}
    if "additionalProperties" in parameters:
        parameters = {
            k: v for k, v in parameters.items() if k != "additionalProperties"
        }
    return Tool(
        name=payload["name"],
        description=payload.get("description", ""),
        input_schema=parameters or None,
    )


def _payload_to_tool_choice(choice: Any) -> dict[str, Any] | None:
    if isinstance(choice, str):
        if choice in ("auto", "none", "required"):
            return {"type": choice}
        return None
    if isinstance(choice, dict):
        return dict(choice)
    return None


def _sse_chunks_to_response(chunks: list[str]) -> dict[str, Any]:
    """Aggregate a stream of Responses SSE chunks into a final resource."""
    completed: dict[str, Any] | None = None
    buffer = "".join(chunks)
    for raw in buffer.split("\n\n"):
        raw = raw.strip("\n")
        if not raw.startswith("event:"):
            continue
        try:
            event_type = raw.split("event:", 1)[1].split("\n", 1)[0].strip()
            data_line = raw.split("data:", 1)[1].strip()
        except IndexError:
            continue
        try:
            payload = json.loads(data_line)
        except json.JSONDecodeError:
            continue
        if event_type in {"response.completed", "response.incomplete"}:
            completed = payload.get("response", payload)
    if completed is None:
        return {
            "id": "",
            "object": "response",
            "status": "failed",
            "error": {"type": "api_error", "message": "No response received."},
            "output": [],
        }
    return completed


def _stored_to_response(stored: StoredResponse) -> dict[str, Any]:
    return {
        "id": stored.id,
        "object": "response",
        "created_at": stored.created_at,
        "completed_at": stored.completed_at,
        "status": stored.status,
        "background": False,
        "error": stored.error,
        "incomplete_details": None,
        "instructions": stored.instructions,
        "metadata": stored.metadata,
        "model": stored.model,
        "output": stored.output,
        "parallel_tool_calls": stored.parallel_tool_calls,
        "previous_response_id": stored.previous_response_id,
        "reasoning": None,
        "store": stored.store,
        "temperature": stored.temperature,
        "text": None,
        "tool_choice": stored.tool_choice,
        "tools": stored.tools,
        "top_p": stored.top_p,
        "truncation": "disabled",
        "usage": stored.usage,
        "user": stored.user,
    }
