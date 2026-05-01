"""Room-agent registry, source inspection, and attestation routes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import Response

from .agent_helpers import image_digest
from ..config import Settings
from ..core import Hivemind
from ..sandbox.models import AgentConfig, AgentCreateRequest
from ..sandbox.settings import build_sandbox_settings
from ..tenants import Caller

logger = logging.getLogger(__name__)


def query_token_visible_agent(caller: Caller, agent_id: str) -> bool:
    """Query-token holders can only see room-advertised agents."""
    if caller.role != "query":
        return True
    visible = {caller.constraints.get("scope_agent_id") or ""}
    fixed = caller.constraints.get("fixed_query_agent_id") or ""
    if fixed:
        visible.add(fixed)
    mediator = caller.constraints.get("fixed_mediator_agent_id") or ""
    if mediator:
        visible.add(mediator)
    return agent_id in visible


async def build_agent_attestation(caller: Caller, agent_id: str) -> dict:
    from .. import attestation as _att

    agent = await asyncio.to_thread(caller.hive.agent_store.get, agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    digests = await asyncio.to_thread(
        caller.hive.agent_store.compute_digests, agent_id
    )
    return {
        "agent_id": agent_id,
        "agent": agent.model_dump(),
        "inspection_mode": getattr(agent, "inspection_mode", "full"),
        "files_count": digests["files_count"],
        "files_digest_sha256": digests["files_digest"],
        "attested_files_count": digests["attested_files_count"],
        "attested_files_digest_sha256": digests["attested_files_digest"],
        "image_digest": image_digest(agent.image),
        "attestation": _att.get_bundle(),
    }


def register_agent_registry_routes(
    app: FastAPI,
    settings: Settings,
    requires_role: Callable[..., Callable],
    get_tenant_hive: Callable,
) -> None:
    """Register room-agent read, delete, file, and attest endpoints."""

    @app.post("/v1/_internal/agents/register-image", include_in_schema=False)
    async def register_agent(
        req: AgentCreateRequest,
        hm: Hivemind = Depends(get_tenant_hive),
    ):
        from ..sandbox.backend import _create_runner

        sandbox_settings = build_sandbox_settings(settings)
        runner = _create_runner(sandbox_settings)
        try:
            if not runner.image_exists(req.image):
                raise HTTPException(
                    status_code=400,
                    detail=f"Image not found: {req.image}",
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("Image preflight failed for %s: %s", req.image, e)
            raise HTTPException(
                status_code=503,
                detail="Container backend unavailable for image validation",
            )

        agent_id = uuid4().hex[:12]
        config = AgentConfig(
            agent_id=agent_id,
            name=req.name,
            description=req.description,
            agent_type=req.agent_type,
            image=req.image,
            entrypoint=req.entrypoint,
            memory_mb=min(req.memory_mb, settings.container_memory_mb),
            max_llm_calls=req.max_llm_calls,
            max_tokens=req.max_tokens,
            timeout_seconds=req.timeout_seconds,
        )
        await asyncio.to_thread(hm.agent_store.create, config)

        file_count = 0
        try:
            files = await runner.extract_image_files_async(config.image)
            await asyncio.to_thread(hm.agent_store.save_files, agent_id, files)
            file_count = len(files)
        except Exception as e:
            logger.warning("Failed to extract files from %s: %s", config.image, e)

        return {
            "agent_id": agent_id,
            "name": req.name,
            "files_extracted": file_count,
        }

    @app.get("/v1/room-agents")
    async def list_agents(
        type: str | None = None,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        agents = await asyncio.to_thread(caller.hive.agent_store.list_agents, type)
        if caller.role == "query":
            visible = {caller.constraints.get("scope_agent_id") or ""}
            fixed = caller.constraints.get("fixed_query_agent_id") or ""
            if fixed:
                visible.add(fixed)
            mediator = caller.constraints.get("fixed_mediator_agent_id") or ""
            if mediator:
                visible.add(mediator)
            agents = [a for a in agents if a.agent_id in visible]
        return [a.model_dump() for a in agents]

    @app.get("/v1/room-agents/{agent_id}")
    async def get_agent(
        agent_id: str,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        if not query_token_visible_agent(caller, agent_id):
            raise HTTPException(404, "Agent not found")
        agent = await asyncio.to_thread(caller.hive.agent_store.get, agent_id)
        if not agent:
            raise HTTPException(404, "Agent not found")
        return agent.model_dump()

    @app.delete("/v1/room-agents/{agent_id}")
    async def delete_agent(
        agent_id: str, hm: Hivemind = Depends(get_tenant_hive)
    ):
        if not await asyncio.to_thread(hm.agent_store.delete, agent_id):
            raise HTTPException(404, "Agent not found")
        return {"status": "ok"}

    @app.get("/v1/room-agents/{agent_id}/files")
    async def list_agent_files(
        agent_id: str,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        if not query_token_visible_agent(caller, agent_id):
            raise HTTPException(404, "Agent not found")
        agent = await asyncio.to_thread(caller.hive.agent_store.get, agent_id)
        if not agent:
            raise HTTPException(404, "Agent not found")
        files = await asyncio.to_thread(
            caller.hive.agent_store.list_file_paths, agent_id
        )
        return {"agent_id": agent_id, "files": files}

    @app.get("/v1/room-agents/{agent_id}/files/{file_path:path}")
    async def read_agent_file(
        agent_id: str,
        file_path: str,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        from ..sandbox.agents import AgentSealedReadError

        if not query_token_visible_agent(caller, agent_id):
            raise HTTPException(404, "Agent not found")
        try:
            content = await asyncio.to_thread(
                caller.hive.agent_store.read_file, agent_id, file_path
            )
        except AgentSealedReadError:
            raise HTTPException(
                status_code=403,
                detail=(
                    "agent is sealed (inspection_mode=sealed); source "
                    "files are encrypted for runtime-only use and cannot "
                    "be read through this endpoint by anyone, including "
                    "the room owner. Image digest, attested files digest, "
                    "and file path list remain inspectable."
                ),
            )
        if content is None:
            raise HTTPException(404, "File not found")
        return Response(content=content, media_type="text/plain; charset=utf-8")

    @app.get("/v1/room-agents/{agent_id}/attest")
    async def attest_agent(
        agent_id: str,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        if not query_token_visible_agent(caller, agent_id):
            raise HTTPException(404, "Agent not found")
        return await build_agent_attestation(caller, agent_id)

    @app.get("/v1/_internal/scope-attest", include_in_schema=False)
    async def scope_attest(
        scope_agent_id: str | None = None,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        if caller.role == "query":
            scope_id = caller.constraints.get("scope_agent_id") or ""
            if not scope_id:
                raise HTTPException(
                    500, "query token missing scope_agent_id constraint"
                )
        else:
            scope_id = (scope_agent_id or "").strip()
            if not scope_id:
                raise HTTPException(
                    400,
                    "owner must pass ?scope_agent_id=… (no token binding)",
                )
        if not query_token_visible_agent(caller, scope_id):
            raise HTTPException(404, "Agent not found")
        body = await build_agent_attestation(caller, scope_id)
        return {"scope_agent_id": scope_id, **body}
