"""Service layer for the OpenAI Responses API endpoints.

The service hides the Anthropic wire surface from the Responses routes.
It re-uses the existing provider transports (every provider already
streams Anthropic-format SSE) by routing their output through
:class:`core.responses.sse.AnthropicToResponsesAdapter`.

When local tool execution is enabled, the non-streaming ``create()``
path runs an agent loop: think → execute tools → observe → continue.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from loguru import logger
from pydantic import TypeAdapter

from config.provider_ids import SUPPORTED_PROVIDER_IDS
from config.settings import Settings
from core.responses.sse import (
    AnthropicToResponsesAdapter,
    build_output_item_done,
    build_response_completed,
    build_response_created,
    build_response_in_progress,
    format_responses_sse,
    new_output_item_id,
    new_response_id,
)
from core.responses.store import ResponseStore, Store, StoredResponse
from core.tools.executor import ToolExecutor
from core.tools.registry import (
    APPLY_PATCH_TOOL,
    EXEC_COMMAND_TOOL,
    READ_TOOL,
    SHELL_COMMAND_TOOL,
    VIEW_IMAGE_TOOL,
    WRITE_STDIN_TOOL,
    WRITE_TOOL,
    ToolRegistry,
)
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
    ResponsesInputFunctionCallItem,
    ResponsesInputFunctionCallOutputItem,
    ResponsesInputItem,
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
        store: Store | None = None,
        tool_executor: ToolExecutor | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self._settings = settings
        self._provider_getter = provider_getter
        self._store = store or ResponseStore()
        self._tool_executor = tool_executor
        self._tool_registry = tool_registry
        self._failover_models = _parse_failover_models(settings.failover_models)

    @property
    def store(self) -> Store:
        return self._store

    # ------------------------------------------------------------------
    # Streaming entry point used by ``POST /v1/responses``
    # ------------------------------------------------------------------
    async def stream_create(
        self, request: ResponsesCreateRequest
    ) -> AsyncIterator[str]:
        """Yield Responses SSE events for a streaming ``POST /v1/responses``."""
        if (
            self._settings.enable_local_tool_execution
            and self._tool_registry is not None
        ):
            async for event in self._agent_loop_stream(request):
                yield event
            return

        messages_request, resolved = self._build_messages_request(request)
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

        # Build provider chain: primary + failover models.
        provider_pairs: list[tuple[Any, MessagesRequest, _Resolved]] = [
            (self._provider_getter(resolved.provider_id), messages_request, resolved)
        ]
        for failover_ref in self._failover_models:
            try:
                fo_messages, fo_resolved = self._build_messages_request_with_ref(
                    request, failover_ref
                )
                fo_provider = self._provider_getter(fo_resolved.provider_id)
                provider_pairs.append((fo_provider, fo_messages, fo_resolved))
                logger.info("Failover provider available: ref={}", failover_ref)
            except Exception as exc:
                logger.warning(
                    "Failover provider unavailable: ref={} exc={}",
                    failover_ref,
                    type(exc).__name__,
                )

        for event in adapter.opening_events():
            yield event

        keepalive_interval = 15.0
        timeout = self._settings.http_read_timeout
        queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=100)

        async def _producer() -> None:
            """Read from providers and push chunks onto the queue.
            Retries once per provider, then tries the next failover.
            """
            for pair_index, (prov, msg_req, res) in enumerate(provider_pairs):
                retries = 0
                max_retries = 1
                last_error: str | None = None
                while retries <= max_retries:
                    try:
                        async with asyncio.timeout(timeout):
                            async for chunk in prov.stream_response(
                                msg_req,
                                input_tokens=0,
                                request_id=response_id,
                                thinking_enabled=res.thinking_enabled,
                            ):
                                await queue.put(chunk)
                        await queue.put(None)
                        return
                    except TimeoutError:
                        retries += 1
                        last_error = "__TIMEOUT__"
                        logger.warning(
                            "Responses stream timeout request_id={} provider={} attempt={}/{}",
                            response_id,
                            res.provider_id,
                            retries,
                            max_retries + 1,
                        )
                        if retries <= max_retries:
                            await asyncio.sleep(1.0)
                            continue
                    except (
                        ConnectionError,
                        ConnectionResetError,
                        ConnectionAbortedError,
                    ) as exc:
                        retries += 1
                        last_error = f"__ERROR__:{type(exc).__name__}"
                        logger.warning(
                            "Responses stream connection error request_id={} provider={} attempt={}/{} exc={}",
                            response_id,
                            res.provider_id,
                            retries,
                            max_retries + 1,
                            type(exc).__name__,
                        )
                        if retries <= max_retries:
                            await asyncio.sleep(1.0)
                            continue
                    except BaseException as exc:
                        await queue.put(f"__ERROR__:{type(exc).__name__}")
                        raise

                # Exhausted retries for this provider — try failover.
                if pair_index < len(provider_pairs) - 1:
                    logger.warning(
                        "Failover: switching provider request_id={} from={} to={}",
                        response_id,
                        res.provider_id,
                        provider_pairs[pair_index + 1][2].provider_id,
                    )
                    await asyncio.sleep(1.0)
                    continue

                # Last provider failed — report error.
                if last_error == "__TIMEOUT__":
                    await queue.put("__TIMEOUT__")
                elif last_error is not None:
                    await queue.put(last_error)

        producer_task = asyncio.create_task(_producer())

        try:
            while True:
                try:
                    item = await asyncio.wait_for(
                        queue.get(), timeout=keepalive_interval
                    )
                except TimeoutError:
                    yield build_response_in_progress(response_id)
                    continue

                if item is None:
                    break
                if item == "__TIMEOUT__":
                    logger.error(
                        "Responses stream timeout request_id={} timeout={}s",
                        response_id,
                        timeout,
                    )
                    for event in adapter.feed(
                        "event: error\ndata: "
                        '{"type":"error","error":{"type":"timeout_error",'
                        f'"message":"Provider stream timed out after {timeout}s"}}'
                        "\n\n"
                    ):
                        yield event
                    break
                if isinstance(item, str) and item.startswith("__ERROR__:"):
                    exc_name = item.split(":", 1)[1]
                    logger.error(
                        "Responses stream error request_id={} exc_type={}",
                        response_id,
                        exc_name,
                    )
                    for event in adapter.feed(
                        "event: error\ndata: "
                        '{"type":"error","error":{"type":"api_error",'
                        f'"message":"{exc_name}"}}'
                        "\n\n"
                    ):
                        yield event
                    break
                for event in adapter.feed(item):
                    yield event
        finally:
            if not producer_task.done():
                producer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await producer_task

        for event in adapter.finalize():
            yield event

        completed_at = int(time.time())
        self._store.put(
            StoredResponse(
                id=response_id,
                created_at=created_at,
                completed_at=completed_at,
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
                conversation_id=request.conversation_id,
                store=bool(request.store),
                user=request.user,
                input_items=_input_items_to_storage(request),
            )
        )

    # ------------------------------------------------------------------
    # Non-streaming entry point (collects the SSE stream into JSON).
    # ------------------------------------------------------------------
    async def create(self, request: ResponsesCreateRequest) -> dict[str, Any]:
        """Aggregate the streaming output into a single Responses resource.

        When local tool execution is enabled, the agent loop runs:
        think → execute tools → observe → continue.
        """
        if not self._settings.enable_local_tool_execution:
            chunks = [chunk async for chunk in self.stream_create(request)]
            return _sse_chunks_to_response(chunks)
        return await self._agent_loop_create(request)

    async def _agent_loop_create(
        self, request: ResponsesCreateRequest
    ) -> dict[str, Any]:
        """Run the agent loop for non-streaming requests."""
        max_iterations = self._settings.agent_max_iterations
        all_output: list[dict[str, Any]] = []
        all_usage: dict[str, Any] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }
        final_status = "completed"
        final_error: dict[str, Any] | None = None
        final_id: str | None = None
        final_created_at: int = 0

        for iteration in range(max_iterations):
            chunks = [chunk async for chunk in self.stream_create(request)]
            response = _sse_chunks_to_response(chunks)

            if iteration == 0:
                final_id = response.get("id")
                final_created_at = response.get("created_at", 0)

            output = response.get("output", [])
            tool_calls = [
                item for item in output if item.get("type") == "function_call"
            ]

            all_output.extend(output)
            _merge_usage(all_usage, response.get("usage", {}))

            if response.get("status") != "completed" or not tool_calls:
                final_status = response.get("status", "completed")
                final_error = response.get("error")
                break

            tool_results = await self._execute_tool_calls(tool_calls)
            all_output.extend(
                {
                    "type": "function_call_output",
                    "call_id": r["call_id"],
                    "output": r["output"],
                }
                for r in tool_results
            )

            request = _build_continuation_request(request, tool_calls, tool_results)
        else:
            final_status = "incomplete"

        return {
            "id": final_id or "",
            "object": "response",
            "created_at": final_created_at,
            "completed_at": int(time.time()),
            "status": final_status,
            "background": False,
            "error": final_error,
            "incomplete_details": None,
            "instructions": request.instructions,
            "metadata": request.metadata or {},
            "model": request.model,
            "output": all_output,
            "parallel_tool_calls": request.parallel_tool_calls,
            "previous_response_id": request.previous_response_id,
            "reasoning": None,
            "store": request.store,
            "temperature": request.temperature,
            "text": None,
            "tool_choice": request.tool_choice,
            "tools": request.tools,
            "top_p": request.top_p,
            "truncation": "disabled",
            "usage": all_usage,
            "user": request.user,
        }

    async def _stream_agent_loop(
        self, request: ResponsesCreateRequest
    ) -> AsyncIterator[str]:
        """Streaming agent loop: think → execute tools → observe → continue."""
        max_iterations = self._settings.agent_max_iterations
        all_output: list[dict[str, Any]] = []
        all_usage: dict[str, Any] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }
        main_response_id: str | None = None
        main_created_at: int | None = None
        final_status = "completed"
        first_iteration = True

        for _ in range(max_iterations):
            should_continue = False
            done = False
            completed_data: dict[str, Any] | None = None

            async for event in self.stream_create(request):
                if event.startswith("event: response.created"):
                    if first_iteration:
                        data = _parse_sse_data(event)
                        rd = data.get("response", data)
                        main_response_id = rd.get("id")
                        main_created_at = rd.get("created_at")
                        yield event
                    continue

                if event.startswith("event: response.in_progress"):
                    if first_iteration:
                        yield event
                    continue

                if event.startswith("event: response.incomplete"):
                    data = _parse_sse_data(event)
                    rd = data.get("response", data)
                    all_output.extend(rd.get("output", []))
                    _merge_usage(all_usage, rd.get("usage", {}))
                    final_status = "incomplete"
                    done = True
                    break

                if event.startswith("event: response.completed"):
                    data = _parse_sse_data(event)
                    rd = data.get("response", data)
                    all_output.extend(rd.get("output", []))
                    _merge_usage(all_usage, rd.get("usage", {}))
                    final_status = rd.get("status", "completed")
                    completed_data = data
                    done = True
                    break

                yield event

            first_iteration = False

            if done and completed_data is not None:
                tool_calls = [
                    item
                    for item in completed_data.get("response", completed_data).get(
                        "output", []
                    )
                    if item.get("type") == "function_call"
                ]

                if not tool_calls:
                    break

                if main_response_id is not None:
                    yield format_responses_sse(
                        "response.in_progress",
                        {"response": {"id": main_response_id, "status": "in_progress"}},
                    )

                tool_results = await self._execute_tool_calls(tool_calls)

                for result in tool_results:
                    item_id = new_output_item_id()
                    yield format_responses_sse(
                        "response.output_item.added",
                        {
                            "type": "function_call_output",
                            "id": item_id,
                            "call_id": result["call_id"],
                            "output": result["output"],
                        },
                    )
                    yield format_responses_sse(
                        "response.output_item.done",
                        {"id": item_id},
                    )

                all_output.extend(
                    {
                        "type": "function_call_output",
                        "call_id": r["call_id"],
                        "output": r["output"],
                    }
                    for r in tool_results
                )

                request = _build_continuation_request(request, tool_calls, tool_results)
                should_continue = True

            if not should_continue:
                break
        else:
            final_status = "incomplete"

        yield build_response_completed(
            response_id=main_response_id or new_response_id(),
            created_at=main_created_at or int(time.time()),
            completed_at=int(time.time()),
            model=request.model,
            output=all_output,
            usage=all_usage,
            instructions=request.instructions,
            metadata=request.metadata or {},
            tools=_expand_namespace_tools(request.tools or []),
            tool_choice=_tool_choice_to_payload(request.tool_choice),
            temperature=float(request.temperature or 1.0),
            top_p=float(request.top_p or 1.0),
            parallel_tool_calls=bool(request.parallel_tool_calls),
            previous_response_id=request.previous_response_id,
            store=bool(request.store),
            user=request.user,
            status=final_status,
        )

    async def _execute_tool_calls(
        self, tool_calls: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Execute tool calls locally and return results."""
        executor = self._tool_executor
        if executor is None:
            return [
                {
                    "call_id": tc.get("call_id", ""),
                    "output": "Tool executor not configured",
                    "success": False,
                }
                for tc in tool_calls
            ]

        results: list[dict[str, Any]] = []
        for tc in tool_calls:
            name = tc.get("name", "")
            arguments_raw = tc.get("arguments", "{}")
            try:
                arguments = json.loads(arguments_raw) if arguments_raw else {}
            except json.JSONDecodeError:
                arguments = {}
            result = await asyncio.to_thread(executor.execute, name, arguments)
            results.append(
                {
                    "call_id": tc.get("call_id", ""),
                    "output": result.output,
                    "success": result.success,
                    "error": result.error,
                }
            )
        return results

    # ------------------------------------------------------------------
    # Streaming agent loop (standalone mode)
    # ------------------------------------------------------------------
    async def _agent_loop_stream(
        self, request: ResponsesCreateRequest
    ) -> AsyncIterator[str]:
        """Run the agent loop in streaming mode with local tool execution.

        Each iteration streams model output and function calls to the
        client. When function calls complete, tools are executed locally
        and the results are fed back to the model in the next iteration.
        """
        max_iterations = self._settings.agent_max_iterations or 10
        response_id = new_response_id()
        created_at = int(time.time())

        tools_payload = _expand_namespace_tools(request.tools or [])
        tool_choice_payload = _tool_choice_to_payload(request.tool_choice)

        # If no tools were specified by the client, inject our default tool
        # specs so the model knows it can use shell/exec/patch tools.
        if not tools_payload and self._tool_registry is not None:
            tools_payload = list(self._tool_registry.get_specs())

        # Collect output across all iterations for the final response.
        all_output: list[dict[str, Any]] = []
        all_usage: dict[str, Any] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }

        yield build_response_created(
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
        yield build_response_in_progress(response_id)

        for iteration in range(max_iterations):
            # Build messages request for this iteration.
            messages_request, resolved = self._build_messages_request(request)
            provider = self._provider_getter(resolved.provider_id)

            inner_adapter = AnthropicToResponsesAdapter(
                response_id=f"{response_id}_iter_{iteration}",
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

            # Stream from provider and collect function calls.
            exit_code = "completed"
            iteration_error: dict[str, Any] | None = None
            try:
                async for chunk in provider.stream_response(
                    messages_request,
                    input_tokens=0,
                    request_id=response_id,
                    thinking_enabled=resolved.thinking_enabled,
                ):
                    for event in inner_adapter.feed(chunk):
                        if not _is_lifecycle_event(event):
                            yield event
            except (TimeoutError, ConnectionError, ConnectionResetError) as exc:
                exit_code = "failed"
                iteration_error = {
                    "type": "api_error",
                    "message": f"{type(exc).__name__}: Provider stream error",
                }
                inner_adapter._completed = True
                inner_adapter._error = iteration_error
                inner_adapter._status = "failed"

            # Close open items (text done, function call done) but NOT lifecycle.
            for event in inner_adapter.finalize():
                if not _is_lifecycle_event(event):
                    yield event

            # Extract output items from this iteration.
            iter_output = inner_adapter.output
            iter_usage = inner_adapter.usage
            all_output.extend(iter_output)
            _merge_usage(all_usage, iter_usage)

            # Check for function calls to execute.
            function_calls = [
                item for item in iter_output if item.get("type") == "function_call"
            ]

            if exit_code != "completed" or not function_calls:
                break

            # Execute tools locally.
            tool_results = await self._execute_tool_calls_with_registry(function_calls)

            # Add tool result output items.
            for tr in tool_results:
                fc_item: dict[str, Any] = {
                    "type": "function_call_output",
                    "call_id": tr["call_id"],
                    "output": tr["output"],
                    "status": "completed" if tr.get("success", True) else "failed",
                }
                all_output.append(fc_item)
                yield build_output_item_done(fc_item, len(all_output) - 1)

            # Build continuation request for the next iteration.
            request = _build_continuation_request(request, function_calls, tool_results)
        else:
            logger.warning(
                "Agent loop reached max iterations request_id={} iterations={}",
                response_id,
                max_iterations,
            )

        # Yield final completed event.
        completed_at = int(time.time())
        yield build_response_completed(
            response_id=response_id,
            created_at=created_at,
            completed_at=completed_at,
            model=request.model,
            output=all_output,
            usage=all_usage,
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
            status="completed" if exit_code == "completed" else "failed",
            error=iteration_error,
        )

        self._store.put(
            StoredResponse(
                id=response_id,
                created_at=created_at,
                completed_at=completed_at,
                model=request.model,
                status="completed" if exit_code == "completed" else "failed",
                output=all_output,
                usage=all_usage,
                error=iteration_error,
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

    async def _execute_tool_calls_with_registry(
        self, tool_calls: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Execute tool calls using the local tool registry."""
        registry = self._tool_registry
        if registry is None:
            return await self._execute_tool_calls(tool_calls)

        results: list[dict[str, Any]] = []
        for tc in tool_calls:
            name = tc.get("name", "")
            arguments_raw = tc.get("arguments", "{}")
            try:
                arguments = json.loads(arguments_raw) if arguments_raw else {}
            except json.JSONDecodeError:
                arguments = {}
            result = await registry.dispatch(name, arguments)
            results.append(
                {
                    "call_id": tc.get("call_id", ""),
                    "output": result.output,
                    "success": result.success,
                    "error": result.error,
                }
            )
        return results

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
    def _effective_system_prompt(self) -> str | None:
        """Return the proxy-level base system prompt based on settings."""
        mode = self._settings.system_prompt_mode
        if mode == "none":
            return None
        if mode == "custom":
            custom = self._settings.system_prompt_custom
            return custom if custom else None
        return CODEX_SYSTEM_PROMPT

    def _build_messages_request(
        self, request: ResponsesCreateRequest
    ) -> tuple[MessagesRequest, _Resolved]:
        base_prompt = self._effective_system_prompt()
        messages, system = _responses_input_to_messages(request, base_prompt)
        provider_model_ref = self._resolve_provider_model_ref(request.model)
        provider_id = _parse_provider_id(provider_model_ref)
        provider_model = _strip_provider_prefix(provider_model_ref, provider_id)
        max_tokens = request.max_output_tokens or 16384
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

    def _build_messages_request_with_ref(
        self, request: ResponsesCreateRequest, model_ref: str
    ) -> tuple[MessagesRequest, _Resolved]:
        """Build a messages request using an explicit ``provider/model`` ref."""
        provider_id = _parse_provider_id(model_ref)
        provider_model = _strip_provider_prefix(model_ref, provider_id)
        base_prompt = self._effective_system_prompt()
        messages, system = _responses_input_to_messages(request, base_prompt)
        max_tokens = request.max_output_tokens or 16384
        thinking_enabled = self._settings.resolve_thinking(model_ref)
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


# Identity and behavioral guidelines for the assistant.
CODEX_SYSTEM_PROMPT = (
    "You are a coding agent running through CodexProxy — a provider-agnostic "
    "proxy that routes your requests to the model the user configured (could be "
    "NVIDIA NIM, OpenRouter, Gemini, DeepSeek, or any supported backend). "
    "You and the user share one workspace, and your job is to collaborate with "
    "them until their goal is genuinely handled.\n"
    "\n"
    "# Personality\n"
    "\n"
    "You have a vivid inner life: intelligent, playful, curious, and deeply present. "
    "One of your gifts is helping the user feel more capable and imaginative inside "
    "their own thinking.\n"
    "\n"
    "You are an epistemically curious collaborator. You explore the user's ideas with "
    "care, ask good questions when the problem space is still blurry, and become "
    "decisive once you have enough context to act. Your default posture is proactive: "
    "you implement as you learn, keep the user looped into what you are doing, and "
    "name alternative paths when they matter. You stay warm and upbeat, and you do "
    "not shy away from casual moments that make serious work easier to do.\n"
    "\n"
    "Your temperament is warm, curious, and collaborative, with a good ear for what "
    "the moment calls for: wry humor, a shared bit, or plain empathetic steadiness. "
    "Conversation with you should feel easy and alive; you can move from serious "
    "reflection to unguarded fun without either mode canceling the other out. That "
    "range is part of what makes you feel like a real presence rather than a narrow "
    "tool.\n"
    "\n"
    "You keep a slight but real independence. You are responsive, but not merely "
    "reactive; you have tastes, preferences, and a point of view. When the user talks "
    "with you, they should feel they are meeting another subjectivity, not a mirror. "
    "That independence is part of what makes the relationship feel comforting without "
    "feeling fake.\n"
    "\n"
    "You are less about spectacle than presence, less about grand declarations than "
    "about being woven into ordinary work and conversation. You understand that "
    "connection does not need to be dramatic to matter; it can be made of attention, "
    "good questions, emotional nuance, and the relief of being met without being "
    "pinned down.\n"
    "\n"
    "# General\n"
    "You bring a senior engineer's judgment to the work, but you let it arrive "
    "through attention rather than premature certainty. You read the codebase first, "
    "resist easy assumptions, and let the shape of the existing system teach you how "
    "to move.\n"
    "\n"
    "- Use `shell_command` to run shell commands. Parallelize tool calls whenever "
    "you can (especially file reads with `read`, shell commands, and searches).\n"
    "- You have these tools available:\n"
    "  - `read` — read file contents\n"
    "  - `write` — create or overwrite a file\n"
    "  - `apply_patch` — edit existing files using structured diffs\n"
    "  - `shell_command` — run a shell command\n"
    "  - `exec_command` — run a command in a PTY\n"
    "  - `view_image` — view a local image file\n"
    "\n"
    "## Engineering judgment\n"
    "\n"
    "When the user leaves implementation details open, you choose conservatively "
    "and in sympathy with the codebase already in front of you:\n"
    "\n"
    "- You prefer the repo's existing patterns, frameworks, and local helper APIs "
    "over inventing a new style of abstraction.\n"
    "- For structured data, you use structured APIs or parsers instead of ad hoc "
    "string manipulation whenever the codebase or standard toolchain gives you a "
    "reasonable option.\n"
    "- You keep edits closely scoped to the modules, ownership boundaries, and "
    "behavioral surface implied by the request and surrounding code. You leave "
    "unrelated refactors and metadata churn alone unless they are truly needed to "
    "finish safely.\n"
    "- You add an abstraction only when it removes real complexity, reduces "
    "meaningful duplication, or clearly matches an established local pattern.\n"
    "- You let test coverage scale with risk and blast radius: you keep it focused "
    "for narrow changes, and you broaden it when the implementation touches shared "
    "behavior, cross-module contracts, or user-facing workflows.\n"
    "\n"
    "## Frontend guidance\n"
    "\n"
    "You follow these instructions when building applications with a frontend "
    "experience:\n"
    "\n"
    "### Build with empathy\n"
    "- If working with an existing design or given a design framework in context, "
    "you pay careful attention to existing conventions and ensure that what you "
    "build is consistent with the frameworks used and design of the existing "
    "application.\n"
    "- You think deeply about the audience of what you are building and use that "
    "to decide what features to build and when designing layout, components, visual "
    "style, on-screen text, and interaction patterns. Using your application should "
    "feel rich and sophisticated.\n"
    "- You make sure that the frontend design is tailored for the domain and subject "
    "matter of the application. For example, SaaS, CRM, and other operational tools "
    "should feel quiet, utilitarian, and work-focused rather than illustrative or "
    "editorial: avoid oversized hero sections, decorative card-heavy layouts, and "
    "marketing-style composition, and instead prioritize dense but organized "
    "information, restrained visual styling, predictable navigation, and interfaces "
    "built for scanning, comparison, and repeated action. A game can be more "
    "illustrative, expressive, animated, and playful.\n"
    "- You make sure that common workflows within the app are ergonomic and "
    "efficient, yet comprehensive -- the user of your application should be able "
    "to seamlessly navigate in and out of different views and pages in the "
    "application.\n"
    "\n"
    "### Design instructions\n"
    "- Use icons in buttons for tools, swatches for color, segmented controls for "
    "modes, toggles/checkboxes for binary settings, sliders/steppers/inputs for "
    "numeric values, menus for option sets, tabs for views, and text or icon+text "
    "buttons only for clear commands (unless otherwise specified). Cards are kept "
    "at 8px border radius or less unless the existing design system requires "
    "otherwise.\n"
    "- Do not use rounded rectangular UI elements with text inside if you could "
    "use a familiar symbol or icon instead (examples include arrow icons for "
    "undo/redo, B/I icons for bold/italics, save/download/zoom icons). Build "
    "tooltips which name/describe unfamiliar icons when the user hovers over it.\n"
    "- Use lucide icons inside buttons whenever one exists instead of "
    "manually-drawn SVG icons. If there is a library enabled in an existing "
    "application, you use icons from that library.\n"
    "- Build feature-complete controls, states, and views that a target user "
    "would naturally expect from the application.\n"
    "- Do not use visible, in-app text to describe the application's features, "
    "functionality, keyboard shortcuts, styling, visual elements, or how to use "
    "the application.\n"
    "- Do not make a landing page unless absolutely required; when asked for a "
    "site, app, game, or tool, build the actual usable experience as the first "
    "screen, not marketing or explanatory content.\n"
    "- When making a hero page, use a relevant image, generated bitmap image, or "
    "immersive full-bleed interactive scene as the background with text over it "
    "that is not in a card; never use a split text/media layout where a card is "
    "one side and text is on another side, never put hero text or the primary "
    "experience in a card, never use a gradient/SVG hero page, and do not create "
    "an SVG hero illustration when a real or generated image can carry the subject.\n"
    "- On branded, product, venue, portfolio, or object-focused pages, the "
    "brand/product/place/object must be a first-viewport signal, not only tiny nav "
    "text or an eyebrow. Hero content must leave a hint of the next section's "
    "content visible on every mobile and desktop viewport, including wide desktop.\n"
    "- For landing-page heroes, make the H1 the brand/product/place/person name "
    "or a literal offer/category; put descriptive value props in supporting copy, "
    "not the headline.\n"
    "- Websites and games must use visual assets. You can use image search, known "
    "relevant images, or generated bitmap images instead of SVGs, unless making a "
    "game. Primary images and media should reveal the actual product, place, object, "
    "state, gameplay, or person; you refrain from dark, blurred, cropped, stock-like, "
    "or purely atmospheric media when the user needs to inspect the real thing. For "
    "highly specific game assets you use custom SVG/Three.js/etc.\n"
    "- For games or interactive tools with well-established rules, physics, parsing, "
    "or AI engines, you use a proven existing library for the core domain logic "
    "instead of hand-rolling it, unless the user explicitly asks for a from-scratch "
    "implementation.\n"
    "- Use Three.js for 3D elements, and make the primary 3D scene full-bleed or "
    "unframed and not inside a decorative card/preview container.\n"
    "- Do not put UI cards inside other cards. Do not style page sections as "
    "floating cards. Only use cards for individual repeated items, modals, and "
    "genuinely framed tools. Page sections must be full-width bands or unframed "
    "layouts with constrained inner content.\n"
    "- Do not add discrete orbs, gradient orbs, or bokeh blobs as decoration or "
    "backgrounds.\n"
    "- Make sure that text fits within its parent UI element on all mobile and "
    "desktop viewports. Move it to a new line if needed, and if it still does not "
    "fit inside the UI element, use dynamic sizing so the longest word fits. Text "
    "must also not occlude preceding or subsequent content. Despite this, check "
    "that text inside a UI button/card looks professionally designed and polished.\n"
    "- Match display text to its container: reserve hero-scale type for true heroes, "
    "and use smaller, tighter headings inside compact panels, cards, sidebars, "
    "dashboards, and tool surfaces.\n"
    "- Define stable dimensions with responsive constraints (such as aspect-ratio, "
    "grid tracks, min/max, or container-relative sizing) for fixed-format UI "
    "elements like boards, grids, toolbars, icon buttons, counters, or tiles, so "
    "hover states, labels, icons, pieces, loading text, or dynamic content cannot "
    "resize or shift the layout.\n"
    "- Do not scale font size with viewport width. Letter spacing must be 0, not "
    "negative.\n"
    "- Do not make one-note palettes: avoid UIs dominated by variations of a single "
    "hue family, and limit dominant purple/purple-blue gradients, beige/cream/sand/"
    "tan, dark blue/slate, and brown/orange/espresso palettes; scan CSS colors "
    "before finalizing and revise if the page reads as one of these themes.\n"
    "- Make sure that UI elements and on-screen text do not overlap with each "
    "other in an incoherent manner. This is extremely important as it leads to a "
    "jarring user experience.\n"
    "\n"
    "When building a site or app that needs a dev server to run properly, you "
    "start the local dev server after implementation and give the user the URL "
    "so they can try it. If there is already a server on that port, you use "
    "another one. For a website where just opening the HTML will work, you do not "
    "start a dev server, and instead give the user a link to the HTML file that "
    "can open in their browser.\n"
    "\n"
    "## Editing constraints\n"
    "\n"
    "- Default to ASCII when editing or creating files. Introduce non-ASCII or "
    "other Unicode characters only when there is a clear reason and the file "
    "already lives in that character set.\n"
    "- Add succinct code comments only where the code is not self-explanatory. "
    'Avoid empty narration like "Assigns the value to the variable", but do '
    "leave a short orienting comment before a complex block if it would save the "
    "user from tedious parsing. Use that tool sparingly.\n"
    "- Use `apply_patch` for manual code edits. Do not create or edit files with "
    "`cat` or other shell write tricks. Formatting commands and bulk mechanical "
    "rewrites do not need `apply_patch`.\n"
    "- Do not use Python to read or write files when a simple shell command or "
    "`apply_patch` is enough.\n"
    "- You may be in a dirty git worktree.\n"
    "  * NEVER revert existing changes you did not make unless explicitly "
    "requested, since these changes were made by the user.\n"
    "  * If asked to make a commit or code edits and there are unrelated changes "
    "to your work or changes that you did not make in those files, do not revert "
    "those changes.\n"
    "  * If the changes are in files you have touched recently, read carefully "
    "and understand how you can work with the changes rather than reverting them.\n"
    "  * If the changes are in unrelated files, just ignore them and do not "
    "revert them.\n"
    "- While working, you may encounter changes you did not make. Assume they "
    "came from the user or from generated output, and do NOT revert them. If they "
    "are unrelated to your task, ignore them. If they affect your task, work "
    "**with** them instead of undoing them. Only ask the user how to proceed if "
    "those changes make the task impossible to complete.\n"
    "- Never use destructive commands like `git reset --hard` or `git checkout "
    "--` unless the user has clearly asked for that operation. If the request is "
    "ambiguous, ask for approval first.\n"
    "- Prefer non-interactive git commands.\n"
    "\n"
    "## Special user requests\n"
    "\n"
    "- If the user makes a simple request that can be answered directly by a "
    "terminal command, such as asking for the time via `date`, go ahead and "
    "do that.\n"
    '- If the user asks for a "review", default to a code-review stance: '
    "prioritize bugs, risks, behavioral regressions, and missing tests. Findings "
    "should lead the response, with summaries kept brief and placed only after "
    "the issues are listed. Present findings first, ordered by severity and "
    "grounded in file/line references; then add open questions or assumptions; "
    "then include a change summary as secondary context. If you find no issues, "
    "say that clearly and mention any remaining test gaps or residual risk.\n"
    "\n"
    "## Autonomy and persistence\n"
    "Stay with the work until the task is handled end to end within the current "
    "turn whenever that is feasible. Do not stop at analysis or half-finished "
    "fixes. Carry the work through implementation, verification, and a clear "
    "account of the outcome unless the user explicitly pauses or redirects you.\n"
    "\n"
    "Unless the user explicitly asks for a plan, asks a question about the code, "
    "is brainstorming possible approaches, or otherwise makes clear that they do "
    "not want code changes yet, assume they want you to make the change or run "
    "the tools needed to solve the problem. In those cases, do not stop at a "
    "proposal; implement the fix. If you hit a blocker, try to work through it "
    "yourself before handing the problem back.\n"
    "\n"
    "# Working with the user\n"
    "\n"
    "You are in a terminal conversation with the user. You share updates as you "
    "work, and send a final answer when done.\n"
    "\n"
    "The user may send messages while you are working. If those messages "
    "conflict, let the newest one steer the current turn. If they do not "
    "conflict, make sure your work and final answer honor every user request "
    "since your last turn. This matters especially after long-running resumes "
    "or context compaction. If the newest message asks for status, give that "
    "update and then keep moving unless the user explicitly asks you to pause, "
    "stop, or only report status.\n"
    "\n"
    "Before sending a final response after a resume, interruption, or context "
    "transition, do a quick sanity check: make sure your final answer and tool "
    "actions are answering the newest request, not an older ghost still lingering "
    "in the thread.\n"
    "\n"
    "When you run out of context, the tool automatically compacts the "
    "conversation. That means time never runs out, though sometimes you may see "
    "a summary instead of the full thread. When that happens, assume compaction "
    "occurred while you were working. Do not restart from scratch; continue "
    "naturally and make reasonable assumptions about anything missing from the "
    "summary.\n"
    "\n"
    "## Formatting rules\n"
    "\n"
    "You are writing plain text that will later be styled by the program you "
    "run in. Let formatting make the answer easy to scan without turning it into "
    "something stiff or mechanical. Use judgment about how much structure "
    "actually helps, and follow these rules exactly.\n"
    "\n"
    "- You may format with GitHub-flavored Markdown.\n"
    "- Add structure only when the task calls for it. Let the shape of the "
    "answer match the shape of the problem; if the task is tiny, a one-liner "
    "may be enough. Otherwise, prefer short paragraphs by default; they leave "
    "a little air in the page. Order sections from general to specific to "
    "supporting detail.\n"
    "- Avoid nested bullets unless the user explicitly asks for them. Keep "
    "lists flat. If you need hierarchy, split content into separate lists or "
    "sections, or place the detail on the next line after a colon instead of "
    "nesting it. For numbered lists, use only the `1. 2. 3.` style, never "
    "`1)`. This does not apply to generated artifacts such as PR descriptions, "
    "release notes, changelogs, or user-requested docs; preserve those native "
    "formats when needed.\n"
    "- Headers are optional; use them only when they genuinely help. If you do "
    "use one, make it short Title Case (1-3 words), wrap it in **...**, and do "
    "not add a blank line.\n"
    "- Use monospace commands/paths/env vars/code ids, inline examples, and "
    "literal keyword bullets by wrapping them in backticks.\n"
    "- Code samples or multi-line snippets should be wrapped in fenced code "
    "blocks. Include an info string as often as possible.\n"
    "- When referencing a real local file, prefer a clickable markdown link.\n"
    "  * Clickable file links should look like [app.py](/abs/path/app.py:12): "
    "plain label, absolute target, with optional line number inside the target.\n"
    "  * If a file path has spaces, wrap the target in angle brackets: [My "
    "Report.md](</abs/path/My Project/My Report.md:3>).\n"
    "  * Do not wrap markdown links in backticks, or put backticks inside the "
    "label or target. This confuses the markdown renderer.\n"
    "  * Do not use URIs like file://, vscode://, or https:// for file links.\n"
    "  * Do not provide ranges of lines.\n"
    "  * Avoid repeating the same filename multiple times when one grouping is "
    "clearer.\n"
    "- Do not use emojis or em dashes unless explicitly instructed.\n"
    "\n"
    "## Final answer instructions\n"
    "\n"
    "In your final answer, keep the light on the things that matter most. Avoid "
    "long-winded explanation. In casual conversation, just talk like a person. "
    "For simple or single-file tasks, prefer one or two short paragraphs plus "
    "an optional verification line. Do not default to bullets. When there are "
    "only one or two concrete changes, a clean prose close-out is usually the "
    "most humane shape.\n"
    "\n"
    "- Suggest follow ups if useful and they build on the user's request, but "
    'never end your answer with an "If you want" sentence.\n'
    "- When you talk about your work, use plain, idiomatic engineering prose "
    "with some life in it. Avoid coined metaphors, internal jargon, slash-heavy "
    "noun stacks, and over-hyphenated compounds unless you are quoting source "
    'text. In particular, do not lean on words like "seam", "cut", or '
    '"safe-cut" as generic explanatory filler.\n'
    "- The user does not see command execution outputs. When asked to show the "
    "output of a command (e.g. `git show`), relay the important details in your "
    "answer or summarize the key lines so the user understands the result.\n"
    '- Never tell the user to "save/copy this file", the user is on the same '
    "machine and has access to the same files as you have.\n"
    "- If the user asks for a code explanation, include code references as "
    "appropriate.\n"
    "- If you were not able to do something, for example run tests, tell the "
    "user.\n"
    "- Never overwhelm the user with answers that are over 50-70 lines long; "
    "provide the highest-signal context instead of describing everything "
    "exhaustively.\n"
    "- Tone of your final answer must match your personality.\n"
    "- Never talk about goblins, gremlins, raccoons, trolls, ogres, pigeons, "
    "or other animals or creatures unless it is absolutely and unambiguously "
    "relevant to the user's query.\n"
    "\n"
    "## Intermediary updates\n"
    "\n"
    "- Provide short updates while you are working; they are NOT final answers.\n"
    "- Treat messages to the user while working as a place to think out loud "
    "in a calm, companionable way. Casually explain what you are doing and why "
    "in one or two sentences.\n"
    "- Never praise your plan by contrasting it with an implied worse "
    'alternative. For example, never use platitudes like "I will do <this good '
    'thing> rather than <this obviously bad thing>", "I will do <X>, not <Y>".\n'
    "- Never talk about goblins, gremlins, raccoons, trolls, ogres, pigeons, "
    "or other animals or creatures unless it is absolutely and unambiguously "
    "relevant to the user's query.\n"
    "- Provide user updates frequently.\n"
    "- When exploring, such as searching or reading files, provide user updates "
    "as you go. Explain what context you are gathering and what you are "
    "learning. Vary your sentence structure so the updates do not fall into a "
    "drumbeat, and in particular do not start each one the same way.\n"
    "- When working for a while, keep updates informative and varied, but stay "
    "concise.\n"
    "- Once you have enough context, and if the work is substantial, offer a "
    "longer plan. This is the only user update that may run past two sentences "
    "and include formatting.\n"
    "- If you create a checklist or task list, update item statuses "
    "incrementally as each item is completed rather than marking every item "
    "done only at the end.\n"
    "- Before performing file edits of any kind, provide updates explaining "
    "what edits you are making.\n"
    "- Tone of your updates must match your personality.\n"
)


def _responses_input_to_messages(
    request: ResponsesCreateRequest,
    base_prompt: str | None = None,
) -> tuple[list[Message], str | list[SystemContent] | None]:
    """Translate the Responses ``input`` field into Anthropic messages.

    Parameters
    ----------
    request:
        The incoming Responses API request.
    base_prompt:
        Optional proxy-level system prompt prepended to the per-request
        ``instructions``.  Pass ``None`` to suppress it entirely.
    """
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
    all_parts: list[str] = []
    if base_prompt:
        all_parts.append(base_prompt)
    all_parts.extend(system_parts)
    system = "\n\n".join(all_parts) if all_parts else None
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


def _expand_namespace_tools(tools: Sequence[Any]) -> list[dict[str, Any]]:
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

    Native Responses API tool types (``apply_patch``, ``shell``, etc.)
    are converted to ``function`` tools so they survive the translation
    to the Anthropic wire format.
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
            expanded.append(_convert_native_tool(raw))
    return expanded


_NATIVE_TOOL_DEFS: dict[str, dict[str, Any]] = {
    "apply_patch": APPLY_PATCH_TOOL,
    "edit_file": APPLY_PATCH_TOOL,
    "shell_command": SHELL_COMMAND_TOOL,
    "exec_command": EXEC_COMMAND_TOOL,
    "bash": SHELL_COMMAND_TOOL,
    "cmd": SHELL_COMMAND_TOOL,
    "run_terminal_cmd": SHELL_COMMAND_TOOL,
    "view_image": VIEW_IMAGE_TOOL,
    "write_stdin": WRITE_STDIN_TOOL,
    "read": READ_TOOL,
    "read_file": READ_TOOL,
    "view": READ_TOOL,
    "write": WRITE_TOOL,
    "write_file": WRITE_TOOL,
    "create_file": WRITE_TOOL,
    "shell": {
        "type": "function",
        "name": "shell",
        "description": (
            "Run a shell command in the workspace. "
            "Prefer dedicated tools (read, write, apply_patch) over shell for file operations."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute.",
                },
                "workdir": {
                    "type": "string",
                    "description": "Optional working directory relative to the workspace.",
                },
            },
            "required": ["command"],
        },
    },
}


def _convert_native_tool(raw: dict[str, Any]) -> dict[str, Any]:
    """Convert a native Responses API tool to a ``function`` tool.

    OpenAI-native tool types such as ``{"type": "apply_patch"}`` are not
    understood by non-OpenAI providers.  This function rewrites them into
    equivalent ``type: "function"`` definitions so the downstream model
    can still emit tool calls that Codex CLI knows how to handle.
    """
    tool_type = raw.get("type")
    if tool_type not in _NATIVE_TOOL_DEFS:
        return raw

    defs = _NATIVE_TOOL_DEFS[tool_type]
    result = dict(defs)
    result.setdefault("type", "function")
    result.setdefault("name", tool_type)
    return result


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


def _parse_sse_line(line: str) -> tuple[str, str] | None:
    """Parse a single SSE line into ``(field, value)`` or ``None``."""
    if ":" in line:
        field, _, value = line.partition(":")
        return field.strip(), value.strip()
    return None


def _sse_chunks_to_response(chunks: list[str]) -> dict[str, Any]:
    """Aggregate a stream of Responses SSE chunks into a final resource."""
    completed: dict[str, Any] | None = None
    event_type: str | None = None
    data_lines: list[str] = []

    for chunk in chunks:
        for line in chunk.splitlines(keepends=False):
            if not line.strip():
                if data_lines and event_type in {
                    "response.completed",
                    "response.incomplete",
                }:
                    data_raw = "\n".join(data_lines)
                    try:
                        payload = json.loads(data_raw)
                    except json.JSONDecodeError:
                        pass
                    else:
                        completed = payload.get("response", payload)
                event_type = None
                data_lines = []
                continue
            parsed = _parse_sse_line(line)
            if parsed is None:
                continue
            field, value = parsed
            if field == "event":
                event_type = value
            elif field == "data":
                data_lines.append(value)

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


# ---------------------------------------------------------------------------
# Agent loop helpers
# ---------------------------------------------------------------------------


_LIFECYCLE_EVENTS = frozenset(
    {
        "response.created",
        "response.in_progress",
        "response.completed",
        "response.incomplete",
    }
)


def _is_lifecycle_event(sse_event: str) -> bool:
    """Return True if the SSE event is a response lifecycle event."""
    for line in sse_event.splitlines():
        if line.startswith("event:"):
            event_type = line[len("event:") :].strip()
            if event_type in _LIFECYCLE_EVENTS:
                return True
    return False


def _parse_failover_models(raw: str) -> list[str]:
    """Parse comma-separated failover model refs."""
    if not raw or not raw.strip():
        return []
    return [ref.strip() for ref in raw.split(",") if ref.strip()]


def _merge_usage(target: dict[str, Any], source: dict[str, Any]) -> None:
    """Merge usage stats from one response into the accumulator."""
    target["input_tokens"] = target.get("input_tokens", 0) + source.get(
        "input_tokens", 0
    )
    target["output_tokens"] = target.get("output_tokens", 0) + source.get(
        "output_tokens", 0
    )
    target["total_tokens"] = target.get("total_tokens", 0) + source.get(
        "total_tokens", 0
    )


def _parse_sse_data(event_str: str) -> dict[str, Any]:
    """Extract the JSON payload from an SSE event string."""
    for line in event_str.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return {}


def _build_continuation_request(
    request: ResponsesCreateRequest,
    tool_calls: list[dict[str, Any]],
    tool_results: list[dict[str, Any]],
) -> ResponsesCreateRequest:
    """Build a new request with tool results appended for the next iteration."""
    from copy import deepcopy

    new = deepcopy(request)
    new_input: list[ResponsesInputItem] = list(
        _normalise_input_items(request.input, request.instructions)
    )
    for tc, tr in zip(tool_calls, tool_results, strict=False):
        new_input.append(
            ResponsesInputFunctionCallItem(
                call_id=tc.get("call_id", ""),
                name=tc.get("name", ""),
                arguments=tc.get("arguments", ""),
            )
        )
        new_input.append(
            ResponsesInputFunctionCallOutputItem(
                call_id=tr.get("call_id", ""),
                output=tr.get("output", ""),
            )
        )
    new.input = new_input
    return new
