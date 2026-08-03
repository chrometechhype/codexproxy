"""OpenAI Responses protocol adapter."""

from .adapter import OpenAIResponsesAdapter
from .errors import (
    ResponsesConversionError,
    openai_error_payload,
    openai_error_type_for_failure,
    openai_failure_payload,
)
from .models import OpenAIResponsesRequest
from .provider_input import build_responses_provider_request
from .provider_stream import ResponsesProviderStream, ResponsesStreamFailure
from .system_prompt import CODEX_SYSTEM_PROMPT

__all__ = [
    "CODEX_SYSTEM_PROMPT",
    "OpenAIResponsesAdapter",
    "OpenAIResponsesRequest",
    "ResponsesConversionError",
    "ResponsesProviderStream",
    "ResponsesStreamFailure",
    "build_responses_provider_request",
    "openai_error_payload",
    "openai_error_type_for_failure",
    "openai_failure_payload",
]
