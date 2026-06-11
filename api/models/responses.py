"""Pydantic models for API responses."""

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel

from .anthropic import (
    ContentBlockRedactedThinking,
    ContentBlockText,
    ContentBlockThinking,
    ContentBlockToolUse,
)


class TokenCountResponse(BaseModel):
    input_tokens: int


class ModelResponse(BaseModel):
    created_at: str
    display_name: str
    id: str
    type: Literal["model"] = "model"


class ModelsListResponse(BaseModel):
    data: list[ModelResponse]
    first_id: str | None
    has_more: bool
    last_id: str | None


class Usage(BaseModel):
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class MessagesResponse(BaseModel):
    id: str
    model: str
    role: Literal["assistant"] = "assistant"
    content: list[
        ContentBlockText
        | ContentBlockToolUse
        | ContentBlockThinking
        | ContentBlockRedactedThinking
        | dict[str, Any]
    ]
    type: Literal["message"] = "message"
    stop_reason: (
        Literal["end_turn", "max_tokens", "stop_sequence", "tool_use"] | None
    ) = None
    stop_sequence: str | None = None
    usage: Usage


# ---------------------------------------------------------------------------
# OpenAI Responses API request/response models (Phase 3 of codexproxy).
# Mirrors the wire surface Codex CLI sends to /v1/responses.
# ---------------------------------------------------------------------------


class ResponsesInputTextPart(BaseModel):
    type: Literal["input_text", "text"] = "input_text"
    text: str = ""


class ResponsesInputMessage(BaseModel):
    type: Literal["message"] = "message"
    role: Literal["user", "assistant", "system", "developer"] = "user"
    content: str | list[ResponsesInputTextPart | dict[str, Any]] = ""


class ResponsesInputFunctionCallItem(BaseModel):
    type: Literal["function_call"] = "function_call"
    id: str | None = None
    call_id: str | None = None
    name: str
    arguments: str = ""


class ResponsesInputFunctionCallOutputItem(BaseModel):
    type: Literal["function_call_output"] = "function_call_output"
    call_id: str | None = None
    output: str = ""


ResponsesInputItem = (
    str
    | dict[str, Any]
    | ResponsesInputMessage
    | ResponsesInputFunctionCallItem
    | ResponsesInputFunctionCallOutputItem
)


class ResponsesToolFunction(BaseModel):
    type: Literal["function"] = "function"
    name: str
    description: str = ""
    parameters: dict[str, Any] = {}
    strict: bool | None = None


class ResponsesCreateRequest(BaseModel):
    """OpenAI Responses request body (Codex CLI compatible)."""

    model_config = {"extra": "allow"}

    model: str
    input: str | list[ResponsesInputItem] | None = None
    instructions: str | None = None
    stream: bool = False
    store: bool = True
    temperature: float = 1.0
    top_p: float = 1.0
    parallel_tool_calls: bool = True
    previous_response_id: str | None = None
    tools: Sequence[ResponsesToolFunction | dict[str, Any]] = []
    tool_choice: str | dict[str, Any] = "auto"
    metadata: dict[str, Any] = {}
    user: str | None = None
    background: bool = False
    reasoning: dict[str, Any] | None = None
    text: dict[str, Any] | None = None
    truncation: str = "disabled"
    max_output_tokens: int | None = None


class ResponsesInputItemView(BaseModel):
    type: str
    role: str | None = None
    content: Any = None
    id: str | None = None
    call_id: str | None = None
    name: str | None = None
    arguments: str | None = None
    output: str | None = None


class ResponsesInputItemsList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ResponsesInputItemView]
    first_id: str | None = None
    last_id: str | None = None
    has_more: bool = False


class ResponsesModelInfo(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int = 0
    owned_by: str = "codexproxy"


class ResponsesModelsListResponse(BaseModel):
    object: Literal["list"] = "list"
    data: list[ResponsesModelInfo]
