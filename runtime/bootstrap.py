"""Single production composition root for the CodexProxy server."""

import os
from functools import partial
from pathlib import Path

from api.app import create_app
from api.ports import ApiServices
from config.logging_config import configure_logging
from config.paths import server_log_path
from config.settings import Settings
from messaging.transcription import TranscriptionService
from messaging.voice import Transcriber
from providers.admission import ProviderAdmissionController
from providers.base import BaseProvider, ProviderConfig
from providers.nvidia_nim.voice import NvidiaNimTranscriber
from providers.openai_codex import (
    OpenAIAuthManager,
    OpenAICodexProvider,
)
from providers.runtime import ProviderRuntime
from providers.runtime.factory import create_provider

from .application import ApplicationRuntime, RestartCallback
from .asgi import RuntimeASGIApp
from .codex_catalog import CodexModelCatalogPublisher
from .provider_manager import ProviderRuntimeManager


def build_asgi_app(
    settings: Settings,
    restart_callback: RestartCallback | None = None,
) -> RuntimeASGIApp:
    """Construct the complete server application and its resource owner."""
    log_path = Path(os.getenv("LOG_FILE", server_log_path()))
    configure_logging(
        log_path,
        level=settings.log_level,
        verbose_third_party=settings.log_raw_api_payloads,
    )
    openai_auth = OpenAIAuthManager(proxy=settings.openai_proxy)
    openai_factory = partial(_create_openai_provider, auth=openai_auth)
    provider_constructor = partial(
        create_provider,
        injected_factories={"openai": openai_factory},
    )
    runtime_factory = partial(
        ProviderRuntime,
        provider_constructor=provider_constructor,
    )
    provider_manager = ProviderRuntimeManager(
        settings,
        runtime_factory=runtime_factory,
        connected_provider_ids=openai_auth.connected_provider_ids,
        model_catalog_publisher=CodexModelCatalogPublisher(),
    )
    runtime = ApplicationRuntime(
        provider_manager,
        transcriber=_create_transcriber(settings),
        restart_callback=restart_callback,
        connected_accounts={"openai": openai_auth},
    )
    services = ApiServices(
        requests=provider_manager,
        admin=runtime,
        tasks=runtime,
    )
    return RuntimeASGIApp(create_app(services), runtime)


def _create_openai_provider(
    config: ProviderConfig,
    _settings: Settings,
    admission: ProviderAdmissionController,
    *,
    auth: OpenAIAuthManager,
) -> BaseProvider:
    return OpenAICodexProvider(config, auth=auth, admission=admission)


def _create_transcriber(settings: Settings) -> Transcriber | None:
    if not settings.voice_note_enabled:
        return None
    if settings.whisper_device == "nvidia_nim":
        return NvidiaNimTranscriber(
            model=settings.whisper_model,
            api_key=settings.nvidia_nim_api_key,
        )
    return TranscriptionService(
        model=settings.whisper_model,
        device=settings.whisper_device,
        huggingface_api_key=settings.huggingface_api_key,
    )
