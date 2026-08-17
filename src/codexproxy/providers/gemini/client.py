"""Google AI Studio Gemini provider (OpenAI-compatible chat completions)."""

from codexproxy.core.anthropic import ReasoningReplayMode
from codexproxy.providers.admission import ProviderAdmissionController
from codexproxy.providers.base import ProviderConfig
from codexproxy.providers.google_openai import (
    GeminiReasoningEncoder,
    GoogleOpenAIProvider,
    validate_google_extra_body,
)
from codexproxy.providers.openai_chat import (
    OpenAIChatProfile,
    OpenAIChatRequestPolicy,
)

_REQUEST_POLICY = OpenAIChatRequestPolicy(
    provider_name="GEMINI",
    reasoning_replay=ReasoningReplayMode.REASONING_CONTENT,
    include_extra_body=True,
    extra_body_validator=validate_google_extra_body,
)
_PROFILE = OpenAIChatProfile(
    _REQUEST_POLICY,
    GeminiReasoningEncoder(),
)


class GeminiProvider(GoogleOpenAIProvider):
    """Gemini API using ``https://generativelanguage.googleapis.com/v1beta/openai/``."""

    def __init__(
        self, config: ProviderConfig, *, admission: ProviderAdmissionController
    ):
        super().__init__(
            config,
            profile=_PROFILE,
            admission=admission,
        )
