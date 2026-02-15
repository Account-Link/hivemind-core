import asyncio
import json
import logging
from datetime import datetime
from uuid import uuid4

from openai import AsyncOpenAI

from .config import Settings
from .models import (
    QueryRequest,
    QueryResponse,
    StoreRequest,
    StoreResponse,
)
from .sandbox.agents import AgentStore
from .sandbox.backend import SandboxBackend
from .sandbox.models import AgentConfig
from .sandbox.settings import build_sandbox_settings
from .store import RecordStore
from .tools import build_agent_file_tools, build_tools

logger = logging.getLogger(__name__)

MEDIATOR_MIN_TOKENS = 128
MEDIATOR_TOKEN_RESERVE = 512
MAX_SCOPE_RECORD_IDS = 900


class Pipeline:
    """Orchestrates store and query pipelines using Docker agent sandboxes.

    All agents are Docker containers. No in-process LLM calls.
    """

    def __init__(self, settings: Settings, store: RecordStore, agent_store: AgentStore):
        self.settings = settings
        self.store = store
        self.agent_store = agent_store
        self.llm_client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout=settings.llm_timeout_seconds,
        )
        self.llm_model = settings.llm_model
        self._sandbox_settings = build_sandbox_settings(settings)

    # ── Store pipeline ──

    async def run_store(self, req: StoreRequest) -> StoreResponse:
        record_id = uuid4().hex[:12]
        ts = datetime.now()
        metadata = dict(req.metadata)
        index_text = req.index_text

        # Run index agent if specified
        index_agent_id = req.index_agent_id or self.settings.default_index_agent
        # Respect pre-computed index_text even when it's an empty string.
        if index_agent_id and index_text is None:
            agent_config = await asyncio.to_thread(
                self.agent_store.get, index_agent_id
            )
            if agent_config is None:
                raise ValueError(f"Index agent '{index_agent_id}' not found")

            raw = await self._run_agent(
                agent_config=agent_config,
                role="index",
                env={
                    "DOCUMENT_DATA": req.data,
                    "DOCUMENT_METADATA": json.dumps(metadata),
                },
                scope=None,  # index agents get configurable scope
            )
            try:
                data = json.loads(raw.strip())
                if not isinstance(data, dict):
                    raise ValueError("index output must be a JSON object")

                if "index_text" in data:
                    value = data["index_text"]
                    if value is not None and not isinstance(value, str):
                        raise ValueError("index_text must be a string or null")
                    index_text = value
                else:
                    index_text = ""

                # Merge agent-produced metadata
                if "metadata" in data:
                    produced_metadata = data["metadata"]
                    if not isinstance(produced_metadata, dict):
                        raise ValueError("metadata must be an object")
                    metadata.update(produced_metadata)
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                logger.error(
                    "Index agent output not valid JSON (%s, %d chars)",
                    e,
                    len(raw),
                )
                raise ValueError(f"Index agent failed: {e}")

        await asyncio.to_thread(
            self.store.write_record,
            id=record_id,
            data=req.data,
            metadata=metadata,
            index_text=index_text,
            created_at=ts.timestamp(),
        )

        return StoreResponse(
            record_id=record_id,
            created_at=ts,
            metadata=metadata,
        )

    # ── Query pipeline ──

    async def run_query(self, req: QueryRequest) -> QueryResponse:
        # Resolve effective budget: min of per-query cap and global
        global_max = self._sandbox_settings.global_max_tokens
        effective_max = min(req.max_tokens or global_max, global_max)
        remaining = effective_max
        total_tokens = 0
        mediator_agent_id = req.mediator_agent_id or self.settings.default_mediator_agent

        # Stage 0: Scope resolution precedence:
        #   1) explicit scope_agent_id
        #   2) explicit scope list
        #   3) configured default scope agent (only when scope omitted)
        scope = self._normalize_and_validate_scope(req.scope)
        if req.scope_agent_id:
            scope, scope_usage = await self._run_scope_agent(
                req, max_tokens=remaining,
            )
            used = scope_usage.get("total_tokens", 0)
            total_tokens += used
            remaining = max(1, remaining - used)
        elif scope is None and self.settings.default_scope_agent:
            scope, scope_usage = await self._run_scope_agent(
                req, max_tokens=remaining,
            )
            used = scope_usage.get("total_tokens", 0)
            total_tokens += used
            remaining = max(1, remaining - used)

        # Stage 1: Query agent
        query_agent_id = req.query_agent_id or self.settings.default_query_agent
        if not query_agent_id:
            raise ValueError(
                "No query agent specified and no default configured"
            )

        query_max_tokens = remaining
        if mediator_agent_id and remaining > MEDIATOR_MIN_TOKENS:
            reserve = min(
                MEDIATOR_TOKEN_RESERVE,
                max(0, remaining - MEDIATOR_MIN_TOKENS),
            )
            query_max_tokens = max(1, remaining - reserve)

        output, records_accessed, query_usage = await self._run_query_agent(
            query_agent_id=query_agent_id,
            prompt=req.query,
            scope=scope,
            max_tokens=query_max_tokens,
            return_usage=True,
        )
        used = query_usage.get("total_tokens", 0)
        total_tokens += used
        remaining = max(1, remaining - used)

        # Stage 2: Optional mediator
        mediated = False
        if mediator_agent_id:
            if remaining < MEDIATOR_MIN_TOKENS:
                logger.info(
                    "Skipping mediator '%s': insufficient remaining budget (%d < %d)",
                    mediator_agent_id,
                    remaining,
                    MEDIATOR_MIN_TOKENS,
                )
            else:
                try:
                    output, mediator_usage = await self._run_mediator_agent(
                        mediator_agent_id=mediator_agent_id,
                        raw_output=output,
                        prompt=req.query,
                        records_accessed=records_accessed,
                        max_tokens=remaining,
                    )
                except ValueError as e:
                    if "not found" in str(e).lower():
                        raise
                    logger.warning(
                        "Mediator '%s' failed; returning unmediated output: %s",
                        mediator_agent_id,
                        e,
                    )
                else:
                    used = mediator_usage.get("total_tokens", 0)
                    total_tokens += used
                    mediated = True

        return QueryResponse(
            output=output,
            records_accessed=records_accessed,
            mediated=mediated,
            usage={"total_tokens": total_tokens, "max_tokens": effective_max},
        )

    # ── Internal: run agents ──

    async def _run_agent(
        self,
        agent_config: AgentConfig,
        role: str,
        env: dict[str, str],
        scope: list[str] | None = None,
        agent_store_for_bridge=None,
        run_query_fn=None,
        max_calls: int | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Run a Docker agent with scoped tools and return its stdout."""
        tools = build_tools(self.store, scope=scope)
        tool_handlers = {t.name: t.handler for t in tools}

        async def on_tool_call(name: str, args: dict) -> str:
            if name not in tool_handlers:
                return f"Error: unknown tool '{name}'. Available: {', '.join(tool_handlers)}"
            return await asyncio.to_thread(tool_handlers[name], **args)

        backend = SandboxBackend(
            self.llm_client,
            self.llm_model,
            self._sandbox_settings,
            agent_config,
        )

        return await backend.run(
            role=role,
            env=env,
            tools=tools,
            on_tool_call=on_tool_call,
            agent_store=agent_store_for_bridge,
            run_query_fn=run_query_fn,
            max_calls=max_calls,
            max_tokens=max_tokens,
        )

    async def _run_scope_agent(
        self,
        req: QueryRequest,
        max_tokens: int | None = None,
    ) -> tuple[list[str], dict]:
        """Run scope agent to determine record_id whitelist. Returns (record_ids, usage)."""
        scope_agent_id = req.scope_agent_id or self.settings.default_scope_agent
        agent_config = await asyncio.to_thread(
            self.agent_store.get, scope_agent_id
        )
        if agent_config is None:
            raise ValueError(f"Scope agent '{scope_agent_id}' not found")

        query_agent_id = req.query_agent_id or self.settings.default_query_agent
        allowed_query_agent_id = query_agent_id

        # Build simulation function for the scope agent
        async def run_query_fn(
            query_agent_id: str,
            prompt: str,
            scope: list[str],
            max_calls: int,
            max_tokens: int,
        ) -> tuple[str, list[str], dict]:
            if not allowed_query_agent_id:
                raise ValueError("No query agent is configured for scope simulation")
            if query_agent_id != allowed_query_agent_id:
                raise ValueError(
                    f"Simulation is restricted to query agent '{allowed_query_agent_id}'"
                )
            return await self._run_query_agent(
                query_agent_id=query_agent_id,
                prompt=prompt,
                scope=scope,
                max_calls=max_calls,
                max_tokens=max_tokens,
                return_usage=True,
            )

        env = {
            "QUERY_PROMPT": req.query,
            "QUERY_AGENT_ID": query_agent_id or "",
        }

        # Scope agents get full access + extra tools + simulation
        scope_tools = build_tools(self.store, scope=None)

        # Add agent file inspection tools if query agent exists
        if query_agent_id:
            scope_tools.extend(
                build_agent_file_tools(self.agent_store, query_agent_id)
            )

        tool_handlers = {t.name: t.handler for t in scope_tools}

        async def on_tool_call(name: str, args: dict) -> str:
            if name not in tool_handlers:
                return f"Error: unknown tool '{name}'. Available: {', '.join(tool_handlers)}"
            return await asyncio.to_thread(tool_handlers[name], **args)

        backend = SandboxBackend(
            self.llm_client,
            self.llm_model,
            self._sandbox_settings,
            agent_config,
        )

        raw, usage = await backend.run(
            role="scope",
            env=env,
            tools=scope_tools,
            on_tool_call=on_tool_call,
            agent_store=self.agent_store,
            run_query_fn=run_query_fn,
            scope_query_agent_id=allowed_query_agent_id,
            max_tokens=max_tokens,
            return_budget_summary=True,
        )

        try:
            data = json.loads(raw.strip())
            record_ids = data.get("record_ids", [])
            if not isinstance(record_ids, list):
                raise ValueError("record_ids must be a list")
            return self._normalize_and_validate_scope(record_ids), usage
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.error(
                "Scope agent output not valid JSON (%s, %d chars)",
                e,
                len(raw),
            )
            raise ValueError(f"Scope agent failed: {e}")

    @staticmethod
    def _normalize_and_validate_scope(scope: list | None) -> list[str] | None:
        if scope is None:
            return None
        normalized = Pipeline._normalize_scope_record_ids(scope)
        if len(normalized) > MAX_SCOPE_RECORD_IDS:
            raise ValueError(
                "scope exceeds maximum size "
                f"({len(normalized)} > {MAX_SCOPE_RECORD_IDS})"
            )
        return normalized

    @staticmethod
    def _normalize_scope_record_ids(record_ids: list) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for idx, value in enumerate(record_ids):
            if not isinstance(value, str):
                raise ValueError(f"record_ids[{idx}] must be a string")
            record_id = value.strip()
            if not record_id:
                raise ValueError(f"record_ids[{idx}] must not be empty")
            if record_id in seen:
                continue
            seen.add(record_id)
            normalized.append(record_id)
        return normalized

    async def _run_query_agent(
        self,
        query_agent_id: str,
        prompt: str,
        scope: list[str] | None = None,
        max_calls: int | None = None,
        max_tokens: int | None = None,
        return_usage: bool = False,
    ) -> tuple[str, list[str]] | tuple[str, list[str], dict]:
        """Run query agent, return output + records, and optionally usage summary."""
        agent_config = await asyncio.to_thread(
            self.agent_store.get, query_agent_id
        )
        if agent_config is None:
            raise ValueError(f"Query agent '{query_agent_id}' not found")

        # Build tracked tools for source tracking
        tools = build_tools(self.store, scope=scope)
        tool_handlers = {t.name: t.handler for t in tools}
        records_accessed: list[str] = []
        seen_records: set[str] = set()

        def _track_record(record_id: str | None) -> None:
            if not record_id or record_id in seen_records:
                return
            seen_records.add(record_id)
            records_accessed.append(record_id)

        async def on_tool_call(name: str, args: dict) -> str:
            if name not in tool_handlers:
                return f"Error: unknown tool '{name}'. Available: {', '.join(tool_handlers)}"
            result = await asyncio.to_thread(tool_handlers[name], **args)
            if name == "read" and "record_id" in args:
                if result != "Record not found":
                    _track_record(args["record_id"])
            elif name in {"search", "list"}:
                try:
                    payload = json.loads(result)
                    if isinstance(payload, list):
                        for item in payload:
                            if isinstance(item, dict):
                                _track_record(item.get("id"))
                except (json.JSONDecodeError, TypeError):
                    pass
            return result

        env = {
            "QUERY_PROMPT": prompt,
        }

        backend = SandboxBackend(
            self.llm_client,
            self.llm_model,
            self._sandbox_settings,
            agent_config,
        )

        run_result = await backend.run(
            role="query",
            env=env,
            tools=tools,
            on_tool_call=on_tool_call,
            max_calls=max_calls,
            max_tokens=max_tokens,
            return_budget_summary=return_usage,
        )

        if return_usage:
            output, usage = run_result
            return output, records_accessed, usage
        return run_result, records_accessed

    async def _run_mediator_agent(
        self,
        mediator_agent_id: str,
        raw_output: str,
        prompt: str,
        records_accessed: list[str],
        max_tokens: int | None = None,
    ) -> tuple[str, dict]:
        """Run mediator agent to filter/audit output. Returns (output, usage)."""
        agent_config = await asyncio.to_thread(
            self.agent_store.get, mediator_agent_id
        )
        if agent_config is None:
            raise ValueError(f"Mediator agent '{mediator_agent_id}' not found")

        env = {
            "RAW_OUTPUT": raw_output,
            "QUERY_PROMPT": prompt,
            "RECORDS_ACCESSED": json.dumps(records_accessed),
        }

        # Mediator has NO data access tools
        backend = SandboxBackend(
            self.llm_client,
            self.llm_model,
            self._sandbox_settings,
            agent_config,
        )

        async def noop_tool_call(name: str, args: dict) -> str:
            return "Error: mediator agents have no tool access"

        return await backend.run(
            role="mediator",
            env=env,
            tools=[],
            on_tool_call=noop_tool_call,
            max_tokens=max_tokens,
            return_budget_summary=True,
        )
