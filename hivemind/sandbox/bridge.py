import asyncio
import json
import logging
import secrets
import socket
import time
from typing import Callable
from uuid import uuid4

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request

from ..tools import Tool
from .budget import Budget
from .models import (
    BridgeLLMRequest,
    BridgeLLMResponse,
    BridgeToolRequest,
    BridgeToolResponse,
    OpenAIChatRequest,
    SimulateRequest,
    SimulateResponse,
)

logger = logging.getLogger(__name__)


def _estimate_prompt_tokens(messages: list[dict]) -> int:
    """Conservative token estimate used for preflight budget checks."""
    total_chars = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        else:
            try:
                total_chars += len(json.dumps(content, ensure_ascii=False))
            except Exception:
                total_chars += len(str(content))
    return max(1, total_chars // 3)


class BridgeServer:
    """Ephemeral HTTP server exposing LLM proxy and tools to a sandboxed agent.

    The bridge is the single network exit point for the agent container.
    It serves:
      - POST /llm/chat — passthrough proxy to LLM (budget-enforced)
      - POST /tools/{name} — dispatch to scoped tool handlers
      - GET  /tools — list available tools with schemas
      - GET  /health — liveness check + budget info

    For scope agents (role="scope"), additional endpoints:
      - POST /sandbox/simulate — nested query agent run
      - GET  /sandbox/agents/{id}/files — list agent source files
      - GET  /sandbox/agents/{id}/files/{path} — read agent source file

    All endpoints except /health require a session token.
    """

    def __init__(
        self,
        session_token: str,
        tools: list[Tool],
        on_tool_call: Callable,
        llm_caller: Callable,
        budget: Budget,
        host: str = "127.0.0.1",
        role: str = "query",
        agent_store=None,
        run_query_fn: Callable | None = None,
        scope_query_agent_id: str | None = None,
    ):
        self.session_token = session_token
        self.tools = {t.name: t for t in tools}
        self.on_tool_call = on_tool_call
        self.llm_caller = llm_caller
        self.budget = budget
        self.host = host
        self.role = role
        self.agent_store = agent_store
        self.run_query_fn = run_query_fn
        self.scope_query_agent_id = scope_query_agent_id
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None
        self._sock: socket.socket | None = None
        self.port: int = 0
        self._llm_lock = asyncio.Lock()

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="Hivemind Sandbox Bridge", docs_url=None, redoc_url=None)
        bridge = self

        async def _check_token(request: Request):
            token = (
                request.headers.get("Authorization", "")
                .removeprefix("Bearer ")
                .strip()
            )
            if not secrets.compare_digest(token, bridge.session_token):
                raise HTTPException(status_code=401, detail="Invalid session token")

        def _enforce_scope_query_agent(agent_id: str) -> None:
            allowed = bridge.scope_query_agent_id
            if not allowed:
                raise HTTPException(400, "No query agent available for this scope session")
            if agent_id != allowed:
                raise HTTPException(
                    403,
                    f"Scope session can only access query agent '{allowed}'",
                )

        @app.get("/health")
        async def health():
            return {"status": "ok", "budget": bridge.budget.summary()}

        @app.get("/tools", dependencies=[Depends(_check_token)])
        async def list_tools():
            return [t.to_openai_def() for t in bridge.tools.values()]

        @app.post("/llm/chat", dependencies=[Depends(_check_token)])
        async def llm_chat(req: BridgeLLMRequest) -> BridgeLLMResponse:
            async with bridge._llm_lock:
                planned_prompt_tokens = _estimate_prompt_tokens(req.messages)
                budget_error = bridge.budget.check(
                    planned_prompt_tokens=planned_prompt_tokens,
                    planned_completion_tokens=req.max_tokens,
                )
                if budget_error:
                    raise HTTPException(status_code=429, detail=budget_error)

                kwargs: dict = {
                    "messages": req.messages,
                    "max_tokens": req.max_tokens,
                }
                if req.model is not None:
                    kwargs["model"] = req.model
                if req.temperature is not None:
                    kwargs["temperature"] = req.temperature
                if req.top_p is not None:
                    kwargs["top_p"] = req.top_p

                result = await bridge.llm_caller(**kwargs)

                usage = result.get("usage", {})
                bridge.budget.record(
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                )

                return BridgeLLMResponse(
                    content=result.get("content", ""),
                    usage=usage,
                )

        @app.post("/v1/chat/completions", dependencies=[Depends(_check_token)])
        async def openai_chat_completions(req: OpenAIChatRequest):
            """OpenAI-compatible chat completions endpoint.

            Standard OpenAI SDKs route here via OPENAI_BASE_URL env var.
            Same budget enforcement as /llm/chat.
            """
            async with bridge._llm_lock:
                planned_prompt_tokens = _estimate_prompt_tokens(req.messages)
                max_tokens = req.max_tokens or 4096
                budget_error = bridge.budget.check(
                    planned_prompt_tokens=planned_prompt_tokens,
                    planned_completion_tokens=max_tokens,
                )
                if budget_error:
                    raise HTTPException(status_code=429, detail=budget_error)

                kwargs: dict = {
                    "messages": req.messages,
                    "max_tokens": max_tokens,
                }
                if req.model is not None:
                    kwargs["model"] = req.model
                if req.temperature is not None:
                    kwargs["temperature"] = req.temperature
                if req.top_p is not None:
                    kwargs["top_p"] = req.top_p
                if req.tools is not None:
                    kwargs["tools"] = req.tools
                if req.tool_choice is not None:
                    kwargs["tool_choice"] = req.tool_choice

                result = await bridge.llm_caller(**kwargs)

                usage = result.get("usage", {})
                bridge.budget.record(
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                )

                # Build OpenAI-format message
                message: dict = {"role": "assistant", "content": result.get("content", "")}
                if "tool_calls" in result:
                    message["tool_calls"] = result["tool_calls"]

                prompt_tokens = usage.get("prompt_tokens", 0)
                completion_tokens = usage.get("completion_tokens", 0)

                return {
                    "id": f"chatcmpl-{uuid4().hex[:12]}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": req.model or "default",
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": result.get("finish_reason") or "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                    },
                }

        @app.post("/tools/{tool_name}", dependencies=[Depends(_check_token)])
        async def call_tool(
            tool_name: str, req: BridgeToolRequest
        ) -> BridgeToolResponse:
            if tool_name not in bridge.tools:
                return BridgeToolResponse(
                    result="",
                    error=f"Unknown tool '{tool_name}'. "
                    f"Available: {', '.join(bridge.tools)}",
                )
            try:
                result = await bridge.on_tool_call(tool_name, req.arguments)
                return BridgeToolResponse(result=result)
            except Exception as e:
                logger.warning("Tool %s error: %s", tool_name, e)
                return BridgeToolResponse(result="", error=str(e))

        # ── Scope-agent-only endpoints ──

        if bridge.role == "scope":

            @app.post(
                "/sandbox/simulate",
                dependencies=[Depends(_check_token)],
                response_model=SimulateResponse,
            )
            async def simulate(req: SimulateRequest) -> SimulateResponse:
                if not bridge.run_query_fn:
                    raise HTTPException(
                        500, "Simulation not available (no run_query_fn)"
                    )
                _enforce_scope_query_agent(req.query_agent_id)
                # Pass full remaining budget to simulation
                remaining = bridge.budget.remaining()
                remaining_calls = remaining["calls"]
                remaining_tokens = remaining["tokens"]
                if remaining_calls < 1 or remaining_tokens < 1:
                    raise HTTPException(
                        429, "Insufficient budget for simulation"
                    )

                try:
                    sim_result = await bridge.run_query_fn(
                        query_agent_id=req.query_agent_id,
                        prompt=req.prompt,
                        scope=req.record_ids,
                        max_calls=remaining_calls,
                        max_tokens=remaining_tokens,
                    )

                    usage = None
                    if isinstance(sim_result, tuple) and len(sim_result) == 3:
                        output, records_accessed, usage = sim_result
                    else:
                        output, records_accessed = sim_result

                    if isinstance(usage, dict):
                        bridge.budget.record(
                            calls=int(usage.get("calls", 0) or 0),
                            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
                        )
                    else:
                        # Compatibility fallback when run_query_fn doesn't return usage.
                        bridge.budget.record(
                            calls=remaining_calls,
                            prompt_tokens=remaining_tokens // 2,
                            completion_tokens=remaining_tokens // 2,
                        )

                    return SimulateResponse(
                        output=output,
                        records_accessed=records_accessed,
                    )
                except Exception as e:
                    logger.warning("Simulation failed: %s", e)
                    raise HTTPException(500, f"Simulation failed: {e}")

            @app.get(
                "/sandbox/agents/{agent_id}/files",
                dependencies=[Depends(_check_token)],
            )
            async def list_agent_files(agent_id: str):
                if not bridge.agent_store:
                    raise HTTPException(500, "Agent store not available")
                _enforce_scope_query_agent(agent_id)
                files = await asyncio.to_thread(
                    bridge.agent_store.list_file_paths, agent_id
                )
                return {"files": files}

            @app.get(
                "/sandbox/agents/{agent_id}/files/{file_path:path}",
                dependencies=[Depends(_check_token)],
            )
            async def read_agent_file(agent_id: str, file_path: str):
                if not bridge.agent_store:
                    raise HTTPException(500, "Agent store not available")
                _enforce_scope_query_agent(agent_id)
                content = await asyncio.to_thread(
                    bridge.agent_store.read_file, agent_id, file_path
                )
                if content is None:
                    raise HTTPException(404, "File not found")
                return {"content": content}

        return app

    async def start(self) -> int:
        """Start the bridge server. Returns the port it's listening on."""
        app = self._build_app()

        config = uvicorn.Config(
            app,
            host=self.host,
            port=0,
            log_level="warning",
        )
        self._sock = config.bind_socket()
        self.port = self._sock.getsockname()[1]
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve(sockets=[self._sock]))

        for _ in range(50):  # 5 seconds max
            await asyncio.sleep(0.1)
            if self._server.started:
                logger.info("Bridge server started on %s:%d", self.host, self.port)
                return self.port
            if self._task and self._task.done():
                try:
                    self._task.result()
                except Exception as e:
                    await self.stop()
                    raise RuntimeError(
                        f"Bridge server failed to start on {self.host}:{self.port}"
                    ) from e
                break

        await self.stop()
        raise RuntimeError(
            f"Bridge server did not start within timeout on {self.host}:{self.port}"
        )

    async def stop(self):
        """Shut down the bridge server."""
        if self._server:
            self._server.should_exit = True
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            except Exception as e:
                logger.debug("Bridge server task exited with error during shutdown: %s", e)
        if self._sock:
            try:
                self._sock.close()
            except Exception as e:
                logger.debug("Bridge server socket close failed: %s", e)
            finally:
                self._sock = None
        logger.info("Bridge server stopped")
