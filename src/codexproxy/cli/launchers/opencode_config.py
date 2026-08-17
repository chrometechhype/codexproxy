"""Process-local OpenCode v1 configuration for CDX model routing."""

from dataclasses import dataclass

from codexproxy.core.json_types import JsonObject

from .common import proxy_v1_url
from .model_catalog import ClientModel

OPENCODE_API_KEY_ENV = "CODEX_PROXY_OPENCODE_API_KEY"
OPENCODE_PROVIDER_ID = "codexproxy"


@dataclass(frozen=True, slots=True)
class OpenCodeConfig:
    """Secret-free file and overlay configuration for one OpenCode process."""

    file: JsonObject
    overlay: JsonObject


def build_opencode_config(
    models: tuple[ClientModel, ...], *, proxy_root_url: str
) -> OpenCodeConfig:
    """Translate a non-empty CDX model snapshot into OpenCode v1 config."""

    if not models:
        raise ValueError("OpenCode requires at least one routable CDX model")

    model_config: JsonObject = {
        model.wire_slug: {
            "name": model.display_name,
            "reasoning": model.allows_reasoning,
        }
        for model in models
    }
    provider_config: JsonObject = {
        "name": "CodexProxy",
        "npm": "@ai-sdk/openai",
        "options": {
            "baseURL": proxy_v1_url(proxy_root_url),
            "apiKey": f"{{env:{OPENCODE_API_KEY_ENV}}}",
        },
    }
    default_model = f"{OPENCODE_PROVIDER_ID}/{models[0].wire_slug}"

    return OpenCodeConfig(
        file={
            "provider": {
                OPENCODE_PROVIDER_ID: {
                    **provider_config,
                    "models": model_config,
                }
            }
        },
        overlay={
            "provider": {OPENCODE_PROVIDER_ID: provider_config},
            "enabled_providers": [OPENCODE_PROVIDER_ID],
            "disabled_providers": [],
            "model": default_model,
            "small_model": default_model,
        },
    )
