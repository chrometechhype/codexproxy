"""Pydantic models for the OpenAI Responses API wire surface.

These mirror the OpenAI Responses request/response schema closely enough
to round-trip the fields ``Codex CLI`` sends and that ``codexproxy``
emits back. Fields outside of v0.1 scope are kept loose (``extra="allow"``)
so we accept upstream additions without immediately breaking clients.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class FunctionToolParam(BaseModel):
    """A single function tool definition in the Responses request."""

    model_config = ConfigDict(extra="allow")

    type: Literal["function"] = "function"
    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    strict: bool | None = None


class ToolChoiceFunction(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["function"] = "function"
    name: str


class ToolChoice(BaseModel):
    """``tool_choice`` accepts strings or a structured object."""

    model_config = ConfigDict(extra="allow")

    choice: str | ToolChoiceFunction | None = None


class ResponseTextFormat(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["text"] = "text"


class ResponseInputTextItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["message"] = "message"
    role: Literal["user", "assistant", "system", "developer"] = "user"
    content: str | list[dict[str, Any]] = ""


class ResponseInputFunctionCallItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["function_call"] = "function_call"
    id: str | None = None
    call_id: str | None = None
    name: str
    arguments: str = ""


class ResponseInputFunctionCallOutputItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["function_call_output"] = "function_call_output"
    call_id: str | None = None
    output: str = ""


class ResponseOutputText(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["output_text"] = "output_text"
    text: str = ""
    annotations: list[dict[str, Any]] = Field(default_factory=list)


class ResponseOutputMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["message"] = "message"
    id: str
    role: Literal["assistant"] = "assistant"
    status: Literal["in_progress", "completed", "incomplete"] = "completed"
    content: list[ResponseOutputText] = Field(default_factory=list)


class ResponseOutputFunctionCall(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["function_call"] = "function_call"
    id: str
    call_id: str
    name: str
    arguments: str = ""
    status: Literal["in_progress", "completed", "incomplete"] = "completed"


ResponseOutputItem = ResponseOutputMessage | ResponseOutputFunctionCall | dict[str, Any]


class ResponseUsage(BaseModel):
    model_config = ConfigDict(extra="allow")

    input_tokens: int = 0
    input_tokens_details: dict[str, Any] = Field(default_factory=dict)
    output_tokens: int = 0
    output_tokens_details: dict[str, Any] = Field(default_factory=dict)
    total_tokens: int = 0


class ResponseResource(BaseModel):
    """The full Responses resource returned in JSON or terminal events."""

    model_config = ConfigDict(extra="allow")

    id: str
    object: Literal["response"] = "response"
    created_at: int
    completed_at: int | None = None
    status: Literal[
        "queued", "in_progress", "completed", "incomplete", "failed", "cancelled"
    ] = "in_progress"
    background: bool = False
    error: dict[str, Any] | None = None
    incomplete_details: dict[str, Any] | None = None
    instructions: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    model: str
    output: list[ResponseOutputItem] = Field(default_factory=list)
    parallel_tool_calls: bool = True
    previous_response_id: str | None = None
    reasoning: dict[str, Any] | None = None
    store: bool = True
    temperature: float = 1.0
    text: dict[str, Any] | None = None
    tool_choice: str | dict[str, Any] = "auto"
    tools: list[FunctionToolParam] = Field(default_factory=list)
    top_p: float = 1.0
    truncation: str = "disabled"
    usage: ResponseUsage = Field(default_factory=ResponseUsage)
    user: str | None = None
