"""FastAPI routes for the OpenAI Responses API surface."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger

from api.dependencies import get_settings, require_api_key
from api.models.responses import (
    ResponsesCreateRequest,
    ResponsesInputItemsList,
    ResponsesModelInfo,
    ResponsesModelsListResponse,
)
from api.responses_service import ResponsesService
from config.settings import Settings
from core.responses.sse import RESPONSES_SSE_RESPONSE_HEADERS
from providers.registry import ProviderRegistry

router = APIRouter()


def get_responses_service(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> ResponsesService:
    """Build a :class:`ResponsesService` for the current request."""
    store = getattr(request.app.state, "responses_store", None)
    if store is None:
        from core.responses.store import ResponseStore

        store = ResponseStore()
        request.app.state.responses_store = store
    return ResponsesService(
        settings,
        provider_getter=lambda provider_type: _resolve_provider(
            provider_type, app=request.app, settings=settings
        ),
        store=store,
    )


def _resolve_provider(provider_type: str, *, app: Any, settings: Settings) -> Any:
    from api.dependencies import resolve_provider

    return resolve_provider(provider_type, app=app, settings=settings)


@router.post("/v1/responses", dependencies=[Depends(require_api_key)])
async def create_response(
    body: ResponsesCreateRequest,
    service: ResponsesService = Depends(get_responses_service),
) -> Response:
    """Create a model response (streaming or JSON)."""
    if body.stream:
        generator = service.stream_create(body)
        return StreamingResponse(
            generator,
            media_type="text/event-stream",
            headers=RESPONSES_SSE_RESPONSE_HEADERS,
        )
    response = await service.create(body)
    return JSONResponse(response)


@router.get("/v1/responses/{response_id}", dependencies=[Depends(require_api_key)])
async def get_response(
    response_id: str,
    service: ResponsesService = Depends(get_responses_service),
) -> dict[str, Any]:
    stored = service.get_response(response_id)
    if stored is None:
        raise HTTPException(status_code=404, detail={"error": "Response not found."})
    return stored


@router.get(
    "/v1/responses/{response_id}/input_items",
    dependencies=[Depends(require_api_key)],
)
async def get_response_input_items(
    response_id: str,
    service: ResponsesService = Depends(get_responses_service),
) -> ResponsesInputItemsList:
    items = service.get_input_items(response_id)
    if items is None:
        raise HTTPException(status_code=404, detail={"error": "Response not found."})
    return items


@router.post("/v1/conversations", dependencies=[Depends(require_api_key)])
async def create_conversation() -> dict[str, Any]:
    """Stub: conversations are not stored in v0.1; return an empty resource."""
    return {
        "id": f"conv_{int(time.time())}",
        "object": "conversation",
        "created_at": int(time.time()),
    }


@router.get("/v1/responses", dependencies=[Depends(require_api_key)])
async def list_responses() -> dict[str, Any]:
    """Stub list view (v0.1 only retains responses in process memory)."""
    return {"object": "list", "data": [], "has_more": False}


@router.get("/v1/models", response_model=ResponsesModelsListResponse)
async def list_models(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> ResponsesModelsListResponse:
    """List models in the Responses-shape expected by Codex CLI."""
    registry: ProviderRegistry | None = getattr(
        request.app.state, "provider_registry", None
    )
    seen: set[str] = set()
    models: list[ResponsesModelInfo] = []
    models.append(
        ResponsesModelInfo(
            id=settings.model,
            created=0,
            owned_by=_owned_by_for_model(settings.model),
        )
    )
    seen.add(settings.model)
    if registry is not None:
        for provider_id, ids in registry.cached_model_ids().items():
            for model_id in sorted(ids):
                ref = f"{provider_id}/{model_id}"
                if ref in seen:
                    continue
                seen.add(ref)
                models.append(
                    ResponsesModelInfo(
                        id=ref,
                        created=0,
                        owned_by=provider_id,
                    )
                )
    logger.debug("models.list responses-shape count={}", len(models))
    return ResponsesModelsListResponse(object="list", data=models)


def _owned_by_for_model(model_ref: str) -> str:
    if "/" in model_ref and model_ref.split("/", 1)[0]:
        return model_ref.split("/", 1)[0]
    return "codexproxy"
