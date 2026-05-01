import asyncio
import json
import logging
import secrets
import shutil
import tarfile
import tempfile
import threading
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from .api.admin_tenants import register_admin_tenant_routes
from .api.agent_registry import register_agent_registry_routes
from .api.agent_helpers import (
    MAX_UPLOAD_SIZE,
    image_digest as _server_image_digest,
    read_extracted_files as _read_extracted_files,
    read_upload_bytes_limited as _read_upload_bytes_limited,
    safe_extract_tar as _safe_extract_tar,
    spawn_bg as _spawn_bg,
    tenant_image_tag as _tenant_image_tag,
    validate_inspection_mode as _validate_inspection_mode,
)
from .api.billing import register_admin_billing_routes, register_owner_billing_routes
from .api.runs import register_run_routes
from .api.room_helpers import (
    apply_room_to_query_request as _apply_room_to_query_request,
    load_room_for_caller as _load_room_for_caller,
    room_prompt_for_run as _room_prompt_for_run,
    room_query_inspection_mode as _room_query_inspection_mode,
    room_wrap_id as _room_wrap_id,
    validate_room_provider as _validate_room_provider,
)
from .api.rooms import register_room_routes
from .api.signup import register_signup_routes
from .api.system import register_system_routes
from .api.tenant_owner import register_tenant_owner_routes
from .config import Settings
from .core import Hivemind
from .models import (
    IndexRequest,
    IndexResponse,
    QueryRequest,
    StoreRequest,
    StoreResponse,
)
from .room_vault import RoomVaultSealed
from .sandbox.settings import build_sandbox_settings
from .tenants import Caller, Role, TenantRegistry
from .version import APP_VERSION

logger = logging.getLogger(__name__)

# Backward-compatible private helper export used by older tests/tools.
_image_digest = _server_image_digest


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Strong-ref set for fire-and-forget tasks so the event loop can't
        # GC them mid-flight (per asyncio docs) and so we can cancel them
        # cleanly on shutdown. Spawning code uses _spawn_bg below.
        background_tasks: set[asyncio.Task] = set()
        app.state.background_tasks = background_tasks

        # Fetch TDX quote + measurements for /v1/attestation. Cheap,
        # cached for process lifetime; falls back to ready=false outside
        # a TEE so local dev boots normally.
        try:
            from . import attestation
            await asyncio.to_thread(attestation.bootstrap)
        except Exception as e:
            logger.warning("attestation bootstrap raised: %s", e)

        # Kick off agent-base image provisioning in the background — do
        # NOT block HTTP readiness on it. GHCR pull can fail (private
        # repo, network blip) and fall back to a multi-minute inline
        # Dockerfile build that will OOM-kill a 2GB CVM. When that ran
        # under `await`, lifespan never completed and uvicorn served
        # "Empty reply from server" until the whole container restart-
        # looped. Uploading agents before this task completes returns
        # the usual "agent-base not present" error — acceptable vs. a
        # hung control plane.
        async def _bootstrap_agent_base():
            try:
                from .agent_base_bootstrap import ensure_agent_base_image
                await asyncio.to_thread(ensure_agent_base_image)
            except Exception as e:
                logger.warning("agent-base bootstrap raised: %s", e)

        agent_base_task = _spawn_bg(app, _bootstrap_agent_base())

        registry = TenantRegistry(settings)
        app.state.registry = registry
        app.state.agent_base_task = agent_base_task
        yield
        # Cancel + drain any in-flight pipeline / upload runs before we
        # shut down the registry, so they don't crash mid-DB-call writing
        # against a closed connection.
        if background_tasks:
            for t in list(background_tasks):
                t.cancel()
            await asyncio.gather(*background_tasks, return_exceptions=True)
        # Close per-tenant Hivemind instances + control DB.
        await asyncio.to_thread(registry.close)

    app = FastAPI(title="Hivemind Core", version=APP_VERSION, lifespan=lifespan)

    # Translate TenantSealed (raised when an operation needs the
    # tenant's DEK but no valid bearer has thawed it since process
    # start) to a clear 503. Capability-token holders see this until
    # the owner interacts with the system after a redeploy.
    from .seal import TenantSealed as _TenantSealed
    from fastapi.responses import JSONResponse as _JSONResponse

    @app.exception_handler(_TenantSealed)
    async def _on_tenant_sealed(_request, exc):  # pragma: no cover
        return _JSONResponse(
            status_code=503,
            content={
                "detail": (
                    "Tenant is sealed: encrypted data cannot be read "
                    "until the owner (hmk_) interacts after the last "
                    "process restart. Have the tenant owner make any "
                    "request, then retry."
                ),
                "error": str(exc),
            },
        )

    @app.exception_handler(RoomVaultSealed)
    async def _on_room_vault_sealed(_request, exc):  # pragma: no cover
        return _JSONResponse(
            status_code=503,
            content={
                "detail": (
                    "Room data is sealed: encrypted room data cannot be "
                    "read until a room participant presents a bearer token "
                    "that has a key wrap for this room."
                ),
                "error": str(exc),
            },
        )

    cors_origins = [
        origin.strip()
        for origin in (settings.cors_allow_origins or "").split(",")
        if origin.strip()
    ]
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    def _registry(request: Request) -> TenantRegistry:
        return request.app.state.registry

    def _bearer(request: Request) -> str:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Unauthorized")
        return auth.removeprefix("Bearer ").strip()

    async def _payer_for_request(
        request: Request,
        caller: Caller,
        *,
        billable_role: str,
    ) -> dict:
        """Resolve who pays for this run.

        Query-token calls attach the participant tenant's ``hmk_`` API key so
        the data owner is not charged for the participant's LLM spend. Owner
        calls default to the owner tenant. Query-token calls without a tenant
        API key are rejected before work starts; the credit-enforcement setting
        controls whether a known payer must have enough positive balance, not
        whether a payer is required.
        """
        payer_key = (
            request.headers.get("X-Hivemind-Api-Key")
            or request.headers.get("X-Hivemind-Payer-Key")
            or ""
        ).strip()
        if payer_key:
            payer = await asyncio.to_thread(
                _registry(request).resolve_payer_key,
                payer_key,
            )
            if payer is None:
                raise HTTPException(401, "invalid tenant API key")
            return {
                "payer_tenant_id": payer["tenant_id"],
                "payer_token_id": payer.get("payer_token_id") or "",
                "billable_role": billable_role,
            }
        if caller.role == "owner":
            return {
                "payer_tenant_id": caller.tenant_id,
                "payer_token_id": "",
                "billable_role": billable_role,
            }
        raise HTTPException(
            402,
            "room invite queries require a tenant API key so usage can be "
            "charged to the querying tenant. In the CLI, run "
            "`hivemind --profile NAME init --service URL --api-key hmk_...` "
            "and then retry with `hivemind --profile NAME room ask ...`.",
        )

    def _billing_provider_for_room(req_provider: str | None, room: dict | None) -> str:
        if room is None:
            return (req_provider or "openrouter").strip().lower()
        allowed = [
            p.strip().lower()
            for p in room.get("allowed_llm_providers") or []
            if p.strip()
        ]
        if not allowed:
            return ""
        return (req_provider or allowed[0]).strip().lower()

    def _billing_models_for_query(hm: Hivemind, req: QueryRequest) -> list[str]:
        roles = ["scope", "query"]
        if req.mediator_agent_id or hm.settings.default_mediator_agent:
            roles.append("mediator")
        models: list[str] = []
        for role in roles:
            model = hm.pipeline._model_for(role, req.model)
            if model and model not in models:
                models.append(model)
        return models

    async def _prepare_billing_hold(
        request: Request,
        caller: Caller,
        hm: Hivemind,
        *,
        run_id: str,
        provider: str,
        models: list[str],
        max_tokens: int,
        billable_role: str,
    ) -> dict:
        payer = await _payer_for_request(
            request,
            caller,
            billable_role=billable_role,
        )
        hold = {"hold_micro_usd": 0, "status": "unbilled"}
        if payer.get("payer_tenant_id"):
            try:
                hold = await asyncio.to_thread(
                    _registry(request).billing_hold_for_run,
                    tenant_id=payer["payer_tenant_id"],
                    payer_token_id=payer.get("payer_token_id") or "",
                    run_id=run_id,
                    provider=provider,
                    models=models,
                    max_tokens=max_tokens,
                    billable_role=billable_role,
                    enforce=settings.billing_enforce_credits,
                )
            except ValueError as e:
                detail = str(e)
                status = 402 if "insufficient billing credit" in detail else 400
                raise HTTPException(status, detail)
        return {
            **payer,
            "billing_provider": provider,
            "billing_model": ",".join(models),
            "billing_hold_micro_usd": int(hold.get("hold_micro_usd") or 0),
            "billing_status": hold.get("status") or "unbilled",
        }

    async def _settle_empty_billing(
        hm: Hivemind,
        run_id: str,
        billing: dict,
        *,
        billable_role: str,
    ) -> None:
        if not billing.get("payer_tenant_id") or hm.billing_meter is None:
            return
        usage = {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "max_tokens": 0,
            "stages": {},
        }
        try:
            settlement = await asyncio.to_thread(
                hm.billing_meter.settle_run,
                payer_tenant_id=billing.get("payer_tenant_id"),
                payer_token_id=billing.get("payer_token_id") or "",
                run_id=run_id,
                usage=usage,
                hold_micro_usd=int(billing.get("billing_hold_micro_usd") or 0),
                billable_role=billable_role,
                default_provider=billing.get("billing_provider"),
                default_model=billing.get("billing_model"),
            )
            if hasattr(hm.run_store, "update_usage"):
                await asyncio.to_thread(
                    hm.run_store.update_usage,
                    run_id,
                    usage,
                    billing_cost_micro_usd=int(
                        settlement.get("cost_micro_usd") or 0
                    ),
                    billing_status=settlement.get("billing_status") or "settled",
                    billing_settled_at=settlement.get("settled_at"),
                )
        except Exception as e:
            logger.warning("empty billing settlement failed for %s: %s", run_id, e)

    async def get_caller(request: Request) -> Caller:
        """Auth + role resolution. Recognizes hmk_ (owner) and hmq_
        (query capability) tokens.

        Stashes ``tenant_id`` and ``caller`` on ``request.state`` so
        downstream code (logging, role-specific handlers) can read them
        without re-resolving. 401 on any missing/invalid/revoked token.
        """
        registry = _registry(request)
        token = _bearer(request)
        caller = await asyncio.to_thread(registry.resolve_any, token)
        if caller is None:
            raise HTTPException(status_code=401, detail="Unauthorized")
        request.state.tenant_id = caller.tenant_id
        request.state.caller = caller
        return caller

    def requires_role(*roles: Role):
        """Build a FastAPI dependency that gates by caller role.

        Use as ``Depends(requires_role("owner"))`` or
        ``Depends(requires_role("owner", "query"))``. Returns the resolved
        :class:`Caller` so handlers can read constraints / hive directly.
        """
        allowed = set(roles)

        async def _dep(caller: Caller = Depends(get_caller)) -> Caller:
            if caller.role not in allowed:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"role '{caller.role}' not permitted "
                        f"(need one of: {sorted(allowed)})"
                    ),
                )
            return caller

        return _dep

    async def get_tenant_hive(
        caller: Caller = Depends(requires_role("owner")),
    ) -> Hivemind:
        """Owner-only Hivemind dependency.

        Backward-compatible shim for endpoints that pre-date capability
        tokens — they all run as the tenant owner. New endpoints that
        accept query tokens should depend on :func:`get_caller` or
        :func:`requires_role` directly.
        """
        return caller.hive

    def _require_scope_agent_id(hm: Hivemind, scope_agent_id: str | None) -> str:
        """Resolve the effective scope agent or reject the request up front."""
        resolved = (scope_agent_id or hm.settings.default_scope_agent or "").strip()
        if not resolved:
            raise HTTPException(
                400,
                "scope_agent_id is required (no default scope agent configured)",
            )
        return resolved

    async def _ensure_scope_agent_exists(hm: Hivemind, scope_agent_id: str) -> None:
        agent = await asyncio.to_thread(hm.agent_store.get, scope_agent_id)
        if not agent:
            raise HTTPException(404, f"Scope agent '{scope_agent_id}' not found")

    async def check_admin(request: Request):
        """Gate admin endpoints with the separate HIVEMIND_ADMIN_KEY."""
        if not settings.admin_key:
            raise HTTPException(
                status_code=503,
                detail="Admin API disabled (HIVEMIND_ADMIN_KEY unset)",
            )
        token = _bearer(request)
        if not secrets.compare_digest(token, settings.admin_key):
            raise HTTPException(status_code=401, detail="Unauthorized")

    register_signup_routes(app, settings)
    register_admin_tenant_routes(app, settings, check_admin)
    register_admin_billing_routes(app, check_admin)
    register_owner_billing_routes(app, requires_role)
    register_tenant_owner_routes(app, _bearer, requires_role, get_tenant_hive)
    register_system_routes(app, settings, check_admin, requires_role)
    register_run_routes(app, requires_role)
    register_agent_registry_routes(app, settings, requires_role, get_tenant_hive)

    # ── Internal pipeline primitives ──
    #
    # Room endpoints below are the public execution surface. These lower-level
    # primitives are kept for tests/admin maintenance and are hidden from the
    # generated API schema so new clients do not learn the old generic path.

    @app.post(
        "/v1/_internal/store",
        response_model=StoreResponse,
        include_in_schema=False,
    )
    async def store(
        req: StoreRequest,
        caller: Caller = Depends(requires_role("owner")),
    ):
        try:
            return await caller.hive.pipeline.run_store(req)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    def _force_scope_for_query_token(
        req: QueryRequest, caller: Caller
    ) -> QueryRequest:
        """If caller is a query-token holder, pin scope_agent_id to the
        token's bound scope agent. Owner-supplied scope_agent_id is left
        alone."""
        if caller.role != "query":
            return req
        bound = caller.constraints.get("scope_agent_id") or ""
        if not bound:
            raise HTTPException(
                status_code=500,
                detail="query token missing scope_agent_id constraint",
            )
        # Always overwrite — query tokens cannot bypass their bound scope.
        return req.model_copy(update={"scope_agent_id": bound})

    # ── Query submit (tracked async) ──
    #
    # The only query-execution endpoint. Synchronous ``POST /v1/query``
    # was removed because (a) it doesn't survive the Phala gateway's
    # 60s read timeout and (b) it never produced a Phase 5 signed
    # envelope, so strict-default attestation silently degraded for
    # every URI-based recipient call. Backed by the run_store table —
    # completed rows carry an Ed25519 signature over the run body.
    # Recipients poll status via ``GET /v1/runs/{run_id}``.

    async def _submit_query_run_for_request(
        req: QueryRequest,
        caller: Caller,
        room: dict | None = None,
        request: Request | None = None,
        bearer: str | None = None,
    ) -> dict:
        hm = caller.hive
        query_agent_id = req.query_agent_id or hm.settings.default_query_agent
        scope_agent_id = _require_scope_agent_id(hm, req.scope_agent_id)
        if not query_agent_id:
            raise HTTPException(
                400, "query_agent_id is required (no default configured)"
            )
        await _ensure_scope_agent_exists(hm, scope_agent_id)
        room_vault_items: list[dict] = []
        if room is not None:
            room_vault_items = await asyncio.to_thread(
                hm.room_vault.list_items_for_bearer,
                room["room_id"],
                _room_wrap_id(caller),
                bearer or "",
            )

        run_id = uuid4().hex[:12]
        effective_max_tokens = min(
            req.max_tokens or hm.settings.max_tokens,
            hm.settings.max_tokens,
        )
        billing = {
            "payer_tenant_id": None,
            "payer_token_id": caller.token_id or "",
            "billable_role": "query",
            "billing_provider": _billing_provider_for_room(req.provider, room),
            "billing_model": ",".join(_billing_models_for_query(hm, req)),
            "billing_hold_micro_usd": 0,
            "billing_status": "unbilled",
        }
        if request is not None:
            billing = await _prepare_billing_hold(
                request,
                caller,
                hm,
                run_id=run_id,
                provider=_billing_provider_for_room(req.provider, room),
                models=_billing_models_for_query(hm, req),
                max_tokens=effective_max_tokens,
                billable_role="query",
            )
        await asyncio.to_thread(
            hm.run_store.create, run_id, query_agent_id,
            scope_agent_id=scope_agent_id,
            issuer_token_id=(caller.token_id or None),
            payer_tenant_id=billing.get("payer_tenant_id"),
            payer_token_id=billing.get("payer_token_id"),
            billable_role=billing.get("billable_role"),
            billing_provider=billing.get("billing_provider"),
            billing_model=billing.get("billing_model"),
            billing_hold_micro_usd=int(billing.get("billing_hold_micro_usd") or 0),
            billing_status=billing.get("billing_status") or "unbilled",
            room_id=(room or {}).get("room_id"),
            room_manifest_hash=(room or {}).get("manifest_hash"),
            prompt=_room_prompt_for_run(room, req.query),
            output_visibility=(room or {}).get(
                "output_visibility", "owner_and_querier"
            ),
            artifacts_enabled=bool((room or {}).get("allow_artifacts", True)),
        )

        _spawn_bg(
            app,
            hm.pipeline.run_query_agent_tracked(
                agent_id=query_agent_id,
                run_id=run_id,
                run_store=hm.run_store,
                artifact_store=hm.artifact_store,
                artifact_retention_seconds=hm.settings.artifact_retention_seconds,
                prompt=req.query,
                scope_agent_id=scope_agent_id,
                mediator_agent_id=req.mediator_agent_id,
                max_tokens=req.max_tokens,
                max_calls=req.max_llm_calls,
                timeout_seconds=req.timeout_seconds,
                model=req.model,
                provider=req.provider,
                policy=req.policy,
                room_id=(room or {}).get("room_id"),
                room_manifest_hash=(room or {}).get("manifest_hash"),
                output_visibility=(room or {}).get(
                    "output_visibility", "owner_and_querier"
                ),
                allowed_llm_providers=(room or {}).get("allowed_llm_providers"),
                artifacts_enabled=bool((room or {}).get("allow_artifacts", True)),
                room_vault_items=room_vault_items,
                payer_tenant_id=billing.get("payer_tenant_id"),
                payer_token_id=billing.get("payer_token_id"),
                billable_role=billing.get("billable_role") or "query",
                billing_provider=billing.get("billing_provider"),
                billing_model=billing.get("billing_model"),
                billing_hold_micro_usd=int(
                    billing.get("billing_hold_micro_usd") or 0
                ),
            ),
        )

        return {
            "run_id": run_id,
            "query_agent_id": query_agent_id,
            "scope_agent_id": scope_agent_id,
            "room_id": (room or {}).get("room_id"),
            "status": "pending",
        }

    register_room_routes(
        app,
        settings,
        _bearer,
        requires_role,
        _submit_query_run_for_request,
    )

    @app.post("/v1/_internal/query/run/submit", include_in_schema=False)
    async def submit_query_run(
        req: QueryRequest,
        request: Request,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        """Submit a query for tracked async processing.

        Returns a ``run_id``; the run executes via
        ``run_query_agent_tracked`` so the completed row carries a
        signed attestation envelope.
        """
        room: dict | None = None
        room_id = (req.room_id or "").strip()
        if caller.role == "query" and caller.constraints.get("room_id"):
            room = await _load_room_for_caller(caller, room_id)
            req = _apply_room_to_query_request(req, room)
        elif room_id:
            room = await _load_room_for_caller(caller, room_id)
            req = _apply_room_to_query_request(req, room)
        else:
            req = _force_scope_for_query_token(req, caller)
        return await _submit_query_run_for_request(
            req,
            caller,
            room,
            request=request,
            bearer=_bearer(request),
        )

    @app.post(
        "/v1/_internal/index",
        response_model=IndexResponse,
        include_in_schema=False,
    )
    async def index(req: IndexRequest, hm: Hivemind = Depends(get_tenant_hive)):
        try:
            return await hm.pipeline.run_index(req)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    from .sandbox.models import AgentConfig

    # ── Room agent upload ──

    @app.post("/v1/room-agents")
    async def upload_agent(
        name: str = Form(...),
        archive: UploadFile = File(...),
        description: str = Form(""),
        agent_type: str = Form("query"),
        entrypoint: str | None = Form(None),
        memory_mb: Annotated[int, Form(ge=16)] = 256,
        max_llm_calls: Annotated[int, Form(ge=1)] = 20,
        max_tokens: Annotated[int, Form(ge=1)] = 100_000,
        timeout_seconds: Annotated[int, Form(ge=1)] = 120,
        # JSON-encoded list of file paths to mark non-attestable (e.g.
        # secret prompts, .env). Excluded from attested_files_digest;
        # still bound by image_digest. Defaults to []  (all attestable).
        private_paths: str = Form("[]"),
        # 'full' or 'sealed'. Room query-agent uploads use the room key;
        # reusable room agents are tenant-sealed or KMS-sealed depending on mode.
        inspection_mode: str = Form("full"),
        hm: Hivemind = Depends(get_tenant_hive),
    ):
        try:
            parsed_private = json.loads(private_paths) if private_paths else []
            if not isinstance(parsed_private, list) or not all(
                isinstance(p, str) for p in parsed_private
            ):
                raise ValueError("must be JSON list of strings")
        except (ValueError, json.JSONDecodeError) as e:
            raise HTTPException(
                status_code=400,
                detail=f"private_paths: {e}",
            )
        validated_mode = _validate_inspection_mode(inspection_mode)
        try:
            content = await _read_upload_bytes_limited(
                archive,
                max_bytes=MAX_UPLOAD_SIZE,
            )
        except ValueError as e:
            raise HTTPException(
                status_code=400,
                detail=str(e),
            )

        tmpdir = tempfile.mkdtemp(prefix="hivemind-upload-")
        try:
            _safe_extract_tar(content, tmpdir)
        except (tarfile.TarError, ValueError) as e:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise HTTPException(
                status_code=400,
                detail=f"Invalid archive: {e}",
            )
        except Exception:
            shutil.rmtree(tmpdir, ignore_errors=True)
            logger.exception("Unexpected archive extraction failure")
            raise HTTPException(
                status_code=500,
                detail="Archive extraction failed",
            )

        agent_id = uuid4().hex[:12]
        run_id = uuid4().hex[:12]

        await asyncio.to_thread(hm.run_store.create, run_id, agent_id)

        async def _build_upload_agent():
            try:
                from .sandbox.backend import _create_runner

                sandbox_settings = build_sandbox_settings(settings)
                runner = _create_runner(sandbox_settings)

                await _build_single_agent(
                    runner,
                    tmpdir,
                    agent_id,
                    agent_type,
                    name,
                    description,
                    entrypoint,
                    min(memory_mb, settings.container_memory_mb),
                    max_llm_calls,
                    max_tokens,
                    timeout_seconds,
                    hm,
                    private_paths=parsed_private,
                    inspection_mode=validated_mode,
                )

                await asyncio.to_thread(
                    hm.run_store.update_status, run_id, "completed",
                )
            except Exception as e:
                logger.error("Background agent upload %s failed: %s", run_id, e)
                try:
                    await asyncio.to_thread(
                        hm.run_store.update_status, run_id, "failed",
                        error=str(e)[:500],
                    )
                except Exception:
                    pass
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        _spawn_bg(app, _build_upload_agent())

        return {"agent_id": agent_id, "run_id": run_id, "status": "pending"}

    # ── Internal multi-agent submit ──

    async def _build_single_agent(
        runner,
        tmpdir: str,
        agent_id: str,
        agent_type: str,
        name: str,
        description: str,
        entrypoint: str | None,
        memory_mb: int,
        max_llm_calls: int,
        max_tokens: int,
        timeout_seconds: int,
        hm: Hivemind,
        private_paths: list[str] | None = None,
        inspection_mode: str = "full",
        room_id: str | None = None,
    ) -> str:
        """Build Docker image, register agent, save files. Returns image tag."""
        image_tag = _tenant_image_tag(hm.tenant_id, agent_id)
        await runner.build_image_async(tmpdir, image_tag)

        config = AgentConfig(
            agent_id=agent_id,
            name=name,
            description=description,
            agent_type=agent_type,
            image=image_tag,
            entrypoint=entrypoint,
            memory_mb=memory_mb,
            max_llm_calls=max_llm_calls,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            inspection_mode=inspection_mode,
        )
        await asyncio.to_thread(hm.agent_store.create, config)

        # Persist the upload tmpdir (Dockerfile + source). On Phala, the
        # CVM root FS — including /var/lib/docker — is reinitialized on
        # every compose update, so per-agent images get wiped. Stored
        # build context lets ensure_image_async rebuild from pgdata
        # (FDE-encrypted, governance-gated) on next invocation.
        try:
            files = await asyncio.to_thread(_read_extracted_files, tmpdir)
            await asyncio.to_thread(
                hm.agent_store.save_files,
                agent_id,
                files,
                private_paths or [],
                inspection_mode,
                room_id,
            )
        except Exception as e:
            logger.warning("Failed to save agent files for %s: %s", agent_id, e)

        return image_tag

    @app.post("/v1/_internal/agents/submit", include_in_schema=False)
    async def submit_agents(
        request: Request,
        # Query agent (required)
        query_archive: UploadFile = File(...),
        query_name: str = Form(...),
        query_description: str = Form(""),
        query_entrypoint: str | None = Form(None),
        # Scope agent (optional)
        scope_archive: UploadFile | None = File(None),
        scope_name: str = Form(""),
        scope_entrypoint: str | None = Form(None),
        # Index agent (optional)
        index_archive: UploadFile | None = File(None),
        index_name: str = Form(""),
        index_entrypoint: str | None = Form(None),
        # Shared params
        prompt: str = Form(""),
        memory_mb: Annotated[int, Form(ge=16)] = 256,
        max_llm_calls: Annotated[int, Form(ge=1)] = 20,
        max_tokens: Annotated[int, Form(ge=1)] = 100_000,
        timeout_seconds: Annotated[int, Form(ge=1)] = 120,
        # Mediator (use existing registered agent)
        mediator_agent_id: str | None = Form(None),
        # Index data (required when index_archive is provided)
        document_data: str | None = Form(None),
        document_metadata: str | None = Form(None),
        # LLM routing (optional). ``provider="tinfoil"`` requires the
        # server to have HIVEMIND_TINFOIL_API_KEY configured. Empty
        # falls back to the global default (openrouter).
        model: str | None = Form(None),
        provider: str | None = Form(None),
        policy: str | None = Form(None),
        # Inspection-mode policy applies only to the query agent. The
        # scope/index agents in this endpoint are A's own agents: their
        # source is owner-readable by design (default 'full').
        query_inspection_mode: str = Form("full"),
        caller: Caller = Depends(requires_role("owner")),
    ):
        """Upload query agent (required) + optional scope/index agents,
        build all, then run the full pipeline with tracking."""
        hm = caller.hive

        validated_query_mode = _validate_inspection_mode(query_inspection_mode)
        has_scope_upload = bool(scope_archive and scope_archive.filename)
        default_scope_id = (hm.settings.default_scope_agent or "").strip()
        if not has_scope_upload and not default_scope_id:
            raise HTTPException(
                400,
                "scope_archive or default scope agent is required",
            )
        if not has_scope_upload:
            await _ensure_scope_agent_exists(hm, default_scope_id)

        # Read archives
        try:
            query_bytes = await _read_upload_bytes_limited(
                query_archive, max_bytes=MAX_UPLOAD_SIZE,
            )
        except ValueError as e:
            raise HTTPException(400, f"query_archive: {e}")

        scope_bytes = None
        if scope_archive and scope_archive.filename:
            try:
                scope_bytes = await _read_upload_bytes_limited(
                    scope_archive, max_bytes=MAX_UPLOAD_SIZE,
                )
            except ValueError as e:
                raise HTTPException(400, f"scope_archive: {e}")

        index_bytes = None
        if index_archive and index_archive.filename:
            if not document_data:
                raise HTTPException(
                    400, "document_data is required when index_archive is provided"
                )
            try:
                index_bytes = await _read_upload_bytes_limited(
                    index_archive, max_bytes=MAX_UPLOAD_SIZE,
                )
            except ValueError as e:
                raise HTTPException(400, f"index_archive: {e}")

        # Extract archives to temp dirs
        tmpdirs: list[str] = []
        try:
            query_tmpdir = tempfile.mkdtemp(prefix="hm-query-")
            tmpdirs.append(query_tmpdir)
            _safe_extract_tar(query_bytes, query_tmpdir)

            scope_tmpdir = None
            if scope_bytes:
                scope_tmpdir = tempfile.mkdtemp(prefix="hm-scope-")
                tmpdirs.append(scope_tmpdir)
                _safe_extract_tar(scope_bytes, scope_tmpdir)

            index_tmpdir = None
            if index_bytes:
                index_tmpdir = tempfile.mkdtemp(prefix="hm-index-")
                tmpdirs.append(index_tmpdir)
                _safe_extract_tar(index_bytes, index_tmpdir)
        except (tarfile.TarError, ValueError) as e:
            for d in tmpdirs:
                shutil.rmtree(d, ignore_errors=True)
            raise HTTPException(400, f"Invalid archive: {e}")

        # Generate IDs
        query_agent_id = uuid4().hex[:12]
        scope_agent_id = uuid4().hex[:12] if scope_tmpdir else None
        effective_scope_agent_id = scope_agent_id or default_scope_id
        index_agent_id = uuid4().hex[:12] if index_tmpdir else None
        run_id = uuid4().hex[:12]
        billing_req = QueryRequest(
            query=prompt or "run uploaded query agent",
            mediator_agent_id=mediator_agent_id,
            max_tokens=max_tokens,
            max_llm_calls=max_llm_calls,
            timeout_seconds=timeout_seconds,
            model=model,
            provider=provider,
            policy=policy,
        )
        billing_models = _billing_models_for_query(hm, billing_req)
        if index_agent_id:
            index_model = hm.pipeline._model_for("index", model)
            if index_model not in billing_models:
                billing_models.append(index_model)
        effective_max_tokens = min(
            max_tokens or hm.settings.max_tokens,
            hm.settings.max_tokens,
        )
        billing = await _prepare_billing_hold(
            request,
            caller,
            hm,
            run_id=run_id,
            provider=(provider or "openrouter").strip().lower(),
            models=billing_models,
            max_tokens=effective_max_tokens * (2 if index_agent_id else 1),
            billable_role="query",
        )

        await asyncio.to_thread(
            hm.run_store.create, run_id, query_agent_id,
            scope_agent_id=effective_scope_agent_id,
            index_agent_id=index_agent_id,
            payer_tenant_id=billing.get("payer_tenant_id"),
            payer_token_id=billing.get("payer_token_id"),
            billable_role=billing.get("billable_role"),
            billing_provider=billing.get("billing_provider"),
            billing_model=billing.get("billing_model"),
            billing_hold_micro_usd=int(billing.get("billing_hold_micro_usd") or 0),
            billing_status=billing.get("billing_status") or "unbilled",
        )

        # Run everything in background
        _spawn_bg(
            app,
            _build_and_run_all(
                hm=hm,
                settings=settings,
                run_id=run_id,
                # Query
                query_tmpdir=query_tmpdir,
                query_agent_id=query_agent_id,
                query_name=query_name,
                query_description=query_description,
                query_entrypoint=query_entrypoint,
                # Scope
                scope_tmpdir=scope_tmpdir,
                scope_agent_id=effective_scope_agent_id,
                scope_name=scope_name,
                scope_entrypoint=scope_entrypoint,
                # Index
                index_tmpdir=index_tmpdir,
                index_agent_id=index_agent_id,
                index_name=index_name,
                index_entrypoint=index_entrypoint,
                # Shared
                prompt=prompt,
                memory_mb=memory_mb,
                max_llm_calls=max_llm_calls,
                max_tokens=max_tokens,
                timeout_seconds=timeout_seconds,
                mediator_agent_id=mediator_agent_id,
                document_data=document_data,
                document_metadata=document_metadata,
                model=model,
                provider=provider,
                policy=policy,
                tmpdirs=tmpdirs,
                query_inspection_mode=validated_query_mode,
                payer_tenant_id=billing.get("payer_tenant_id"),
                payer_token_id=billing.get("payer_token_id"),
                billable_role=billing.get("billable_role") or "query",
                billing_provider=billing.get("billing_provider"),
                billing_model=billing.get("billing_model"),
                billing_hold_micro_usd=int(
                    billing.get("billing_hold_micro_usd") or 0
                ),
            ),
        )

        return {
            "run_id": run_id,
            "query_agent_id": query_agent_id,
            "scope_agent_id": effective_scope_agent_id,
            "index_agent_id": index_agent_id,
            "status": "pending",
            "query_inspection_mode": validated_query_mode,
        }

    async def _build_and_run_all(
        hm: Hivemind,
        settings: Settings,
        run_id: str,
        # Query
        query_tmpdir: str,
        query_agent_id: str,
        query_name: str,
        query_description: str,
        query_entrypoint: str | None,
        # Scope
        scope_tmpdir: str | None,
        scope_agent_id: str | None,
        scope_name: str,
        scope_entrypoint: str | None,
        # Index
        index_tmpdir: str | None,
        index_agent_id: str | None,
        index_name: str,
        index_entrypoint: str | None,
        # Shared
        prompt: str,
        memory_mb: int,
        max_llm_calls: int,
        max_tokens: int,
        timeout_seconds: int,
        mediator_agent_id: str | None,
        document_data: str | None,
        document_metadata: str | None,
        model: str | None,
        provider: str | None,
        policy: str | None,
        tmpdirs: list[str],
        query_inspection_mode: str = "full",
        payer_tenant_id: str | None = None,
        payer_token_id: str | None = None,
        billable_role: str = "query",
        billing_provider: str | None = None,
        billing_model: str | None = None,
        billing_hold_micro_usd: int = 0,
    ) -> None:
        """Background: build all agent images in parallel, then run pipeline."""
        import time as _time

        from .sandbox.backend import _create_runner

        billing = {
            "payer_tenant_id": payer_tenant_id,
            "payer_token_id": payer_token_id,
            "billing_provider": billing_provider,
            "billing_model": billing_model,
            "billing_hold_micro_usd": billing_hold_micro_usd,
        }
        try:
            sandbox_settings = build_sandbox_settings(settings)
            runner = _create_runner(sandbox_settings)
            capped_mb = min(memory_mb, settings.container_memory_mb)

            # -- Build stage: build all agents in parallel --
            build_t0 = _time.time()
            await asyncio.to_thread(
                hm.run_store.update_stage, run_id, "build", started_at=build_t0,
            )

            build_tasks = []
            # Query agent (always)
            build_tasks.append(
                _build_single_agent(
                    runner, query_tmpdir, query_agent_id, "query",
                    query_name, query_description, query_entrypoint,
                    capped_mb, max_llm_calls, max_tokens, timeout_seconds, hm,
                    inspection_mode=query_inspection_mode,
                )
            )
            # Scope agent (optional)
            if scope_tmpdir and scope_agent_id:
                build_tasks.append(
                    _build_single_agent(
                        runner, scope_tmpdir, scope_agent_id, "scope",
                        scope_name or f"scope-{scope_agent_id}",
                        "", scope_entrypoint,
                        capped_mb, max_llm_calls, max_tokens, timeout_seconds, hm,
                    )
                )
            # Index agent (optional)
            if index_tmpdir and index_agent_id:
                build_tasks.append(
                    _build_single_agent(
                        runner, index_tmpdir, index_agent_id, "index",
                        index_name or f"index-{index_agent_id}",
                        "", index_entrypoint,
                        capped_mb, max_llm_calls, max_tokens, timeout_seconds, hm,
                    )
                )

            try:
                await asyncio.gather(*build_tasks)
            except Exception as e:
                logger.exception("Image build failed in unified submit %s", run_id)
                await asyncio.to_thread(
                    hm.run_store.update_status, run_id, "failed",
                    error=f"Image build failed: {e}",
                )
                await _settle_empty_billing(
                    hm,
                    run_id,
                    billing,
                    billable_role=billable_role,
                )
                return
            finally:
                for d in tmpdirs:
                    shutil.rmtree(d, ignore_errors=True)
                await asyncio.to_thread(
                    hm.run_store.update_stage, run_id, "build",
                    ended_at=_time.time(),
                )

            # -- Index stage (optional, runs before query) --
            if index_agent_id and document_data:
                try:
                    await asyncio.to_thread(
                        hm.run_store.update_status, run_id, "running",
                    )
                    await hm.pipeline.run_index_tracked(
                        index_agent_id=index_agent_id,
                        run_id=run_id,
                        run_store=hm.run_store,
                        document_data=document_data,
                        document_metadata=document_metadata or "{}",
                        max_tokens=max_tokens,
                        model=model,
                        provider=provider,
                        payer_tenant_id=payer_tenant_id,
                        payer_token_id=payer_token_id,
                        billable_role="index",
                        billing_provider=billing_provider,
                        billing_model=billing_model,
                        billing_hold_micro_usd=0,
                    )
                except Exception as e:
                    logger.warning(
                        "Index agent '%s' failed for run %s; "
                        "continuing with query: %s",
                        index_agent_id, run_id, e,
                    )

            # -- Query pipeline (scope → query → mediator) --
            await hm.pipeline.run_query_agent_tracked(
                agent_id=query_agent_id,
                run_id=run_id,
                run_store=hm.run_store,
                artifact_store=hm.artifact_store,
                artifact_retention_seconds=hm.settings.artifact_retention_seconds,
                prompt=prompt,
                scope_agent_id=scope_agent_id,
                mediator_agent_id=mediator_agent_id,
                max_tokens=max_tokens,
                model=model,
                provider=provider,
                policy=policy,
                payer_tenant_id=payer_tenant_id,
                payer_token_id=payer_token_id,
                billable_role=billable_role,
                billing_provider=billing_provider,
                billing_model=billing_model,
                billing_hold_micro_usd=billing_hold_micro_usd,
            )

        except Exception as e:
            logger.error("Unified submit run %s failed: %s", run_id, e)
            try:
                await asyncio.to_thread(
                    hm.run_store.update_status, run_id, "failed",
                    error=str(e)[:500],
                )
                await _settle_empty_billing(
                    hm,
                    run_id,
                    billing,
                    billable_role=billable_role,
                )
            except Exception:
                pass

    # ── Room query-agent submit + run tracking (async-submit flow) ──

    @app.post("/v1/rooms/{room_id}/query-agents")
    async def submit_query_agent(
        room_id: str,
        request: Request,
        name: str = Form(...),
        archive: UploadFile = File(...),
        prompt: str = Form(""),
        description: str = Form(""),
        entrypoint: str | None = Form(None),
        memory_mb: Annotated[int, Form(ge=16)] = 256,
        max_llm_calls: Annotated[int, Form(ge=1)] = 20,
        max_tokens: Annotated[int, Form(ge=1)] = 100_000,
        timeout_seconds: Annotated[int, Form(ge=1)] = 120,
        mediator_agent_id: str | None = Form(None),
        # LLM routing (optional). ``provider="tinfoil"`` requires the
        # server to have HIVEMIND_TINFOIL_API_KEY configured.
        model: str | None = Form(None),
        provider: str | None = Form(None),
        policy: str | None = Form(None),
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        """Upload query agent source into a room and kick off execution."""
        hm = caller.hive
        room = await _load_room_for_caller(caller, room_id)
        if room.get("query_mode") != "uploadable":
            raise HTTPException(
                403,
                "this room uses a fixed query agent; uploads are disabled",
            )
        if not caller.constraints.get("can_upload_query_agent") and caller.role == "query":
            raise HTTPException(
                status_code=403,
                detail="this room invite may not upload query agents",
            )
        scope_agent_id = room["scope_agent_id"]
        room_policy = room.get("policy") or ""
        requested_policy = (policy or "").strip()
        if requested_policy and requested_policy != room_policy:
            raise HTTPException(
                400,
                "room policy is fixed by the signed room manifest; "
                "caller-supplied policy cannot override it",
            )
        policy = room_policy
        _validate_room_provider(provider, room)
        validated_mode = _validate_inspection_mode(
            _room_query_inspection_mode(room),
            require_kms=False,
        )
        scope_agent_id = _require_scope_agent_id(hm, scope_agent_id)
        await _ensure_scope_agent_exists(hm, scope_agent_id)

        room_vault_items: list[dict] = []
        bearer = _bearer(request)
        await asyncio.to_thread(
            hm.room_vault.open,
            room["room_id"],
            _room_wrap_id(caller),
            bearer,
        )
        room_vault_items = await asyncio.to_thread(
            hm.room_vault.list_items,
            room["room_id"],
        )

        try:
            content = await _read_upload_bytes_limited(
                archive, max_bytes=MAX_UPLOAD_SIZE,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        tmpdir = tempfile.mkdtemp(prefix="hivemind-upload-")
        try:
            _safe_extract_tar(content, tmpdir)
        except (tarfile.TarError, ValueError) as e:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise HTTPException(status_code=400, detail=f"Invalid archive: {e}")

        # Create run record immediately, return fast
        agent_id = uuid4().hex[:12]
        run_id = uuid4().hex[:12]
        billing_req = QueryRequest(
            query=prompt or "run uploaded room query agent",
            query_agent_id=agent_id,
            scope_agent_id=scope_agent_id,
            mediator_agent_id=mediator_agent_id,
            max_tokens=max_tokens,
            max_llm_calls=max_llm_calls,
            timeout_seconds=timeout_seconds,
            model=model,
            provider=provider,
            policy=policy,
        )
        billing = await _prepare_billing_hold(
            request,
            caller,
            hm,
            run_id=run_id,
            provider=_billing_provider_for_room(provider, room),
            models=_billing_models_for_query(hm, billing_req),
            max_tokens=min(max_tokens or hm.settings.max_tokens, hm.settings.max_tokens),
            billable_role="query",
        )
        await asyncio.to_thread(
            hm.run_store.create, run_id, agent_id,
            scope_agent_id=scope_agent_id,
            issuer_token_id=(caller.token_id or None),
            payer_tenant_id=billing.get("payer_tenant_id"),
            payer_token_id=billing.get("payer_token_id"),
            billable_role=billing.get("billable_role"),
            billing_provider=billing.get("billing_provider"),
            billing_model=billing.get("billing_model"),
            billing_hold_micro_usd=int(billing.get("billing_hold_micro_usd") or 0),
            billing_status=billing.get("billing_status") or "unbilled",
            room_id=(room or {}).get("room_id"),
            room_manifest_hash=(room or {}).get("manifest_hash"),
            prompt=_room_prompt_for_run(room, prompt),
            output_visibility=(room or {}).get(
                "output_visibility", "owner_and_querier"
            ),
            artifacts_enabled=bool((room or {}).get("allow_artifacts", True)),
        )

        # Everything else runs in background
        _spawn_bg(
            app,
            _build_and_run(
                hm=hm,
                settings=settings,
                tmpdir=tmpdir,
                agent_id=agent_id,
                run_id=run_id,
                name=name,
                description=description,
                entrypoint=entrypoint,
                memory_mb=memory_mb,
                max_llm_calls=max_llm_calls,
                max_tokens=max_tokens,
                timeout_seconds=timeout_seconds,
                prompt=prompt,
                scope_agent_id=scope_agent_id,
                mediator_agent_id=mediator_agent_id,
                model=model,
                provider=provider,
                policy=policy,
                inspection_mode=validated_mode,
                room=room,
                room_vault_items=room_vault_items,
                payer_tenant_id=billing.get("payer_tenant_id"),
                payer_token_id=billing.get("payer_token_id"),
                billable_role=billing.get("billable_role") or "query",
                billing_provider=billing.get("billing_provider"),
                billing_model=billing.get("billing_model"),
                billing_hold_micro_usd=int(
                    billing.get("billing_hold_micro_usd") or 0
                ),
            ),
        )

        return {
            "run_id": run_id,
            "agent_id": agent_id,
            "room_id": (room or {}).get("room_id"),
            "status": "pending",
            "inspection_mode": validated_mode,
        }

    async def _build_and_run(
        hm: Hivemind,
        settings: Settings,
        tmpdir: str,
        agent_id: str,
        run_id: str,
        name: str,
        description: str,
        entrypoint: str | None,
        memory_mb: int,
        max_llm_calls: int,
        max_tokens: int,
        timeout_seconds: int,
        prompt: str,
        scope_agent_id: str | None,
        mediator_agent_id: str | None,
        model: str | None = None,
        provider: str | None = None,
        policy: str | None = None,
        inspection_mode: str = "full",
        room: dict | None = None,
        room_vault_items: list[dict] | None = None,
        payer_tenant_id: str | None = None,
        payer_token_id: str | None = None,
        billable_role: str = "query",
        billing_provider: str | None = None,
        billing_model: str | None = None,
        billing_hold_micro_usd: int = 0,
    ) -> None:
        """Background task: build image, register agent, run pipeline."""
        from .sandbox.backend import _create_runner

        billing = {
            "payer_tenant_id": payer_tenant_id,
            "payer_token_id": payer_token_id,
            "billing_provider": billing_provider,
            "billing_model": billing_model,
            "billing_hold_micro_usd": billing_hold_micro_usd,
        }
        try:
            # -- Build Docker image --
            import time as _time

            build_t0 = _time.time()
            await asyncio.to_thread(
                hm.run_store.update_stage, run_id, "build", started_at=build_t0,
            )

            sandbox_settings = build_sandbox_settings(settings)
            runner = _create_runner(sandbox_settings)
            image_tag = _tenant_image_tag(hm.tenant_id, agent_id)

            # Capture upload tmpdir (Dockerfile + source) before the
            # finally block rmtree's it — needed for rebuild-from-pgdata
            # after a Phala compose update wipes /var/lib/docker.
            captured_files: dict[str, str] = {}
            try:
                await runner.build_image_async(tmpdir, image_tag)
                try:
                    captured_files = _read_extracted_files(tmpdir)
                except Exception as e:
                    logger.warning(
                        "Failed to read upload context for %s: %s",
                        agent_id, e,
                    )
            except Exception as e:
                logger.exception("Image build failed for agent %s", agent_id)
                await asyncio.to_thread(
                    hm.run_store.update_status, run_id, "failed",
                    error=f"Image build failed: {e}",
                )
                await _settle_empty_billing(
                    hm,
                    run_id,
                    billing,
                    billable_role=billable_role,
                )
                return
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
                await asyncio.to_thread(
                    hm.run_store.update_stage, run_id, "build",
                    ended_at=_time.time(),
                )

            # -- Register agent --
            config = AgentConfig(
                agent_id=agent_id,
                name=name,
                description=description,
                agent_type="query",
                image=image_tag,
                entrypoint=entrypoint,
                memory_mb=min(memory_mb, settings.container_memory_mb),
                max_llm_calls=max_llm_calls,
                max_tokens=max_tokens,
                timeout_seconds=timeout_seconds,
                inspection_mode=inspection_mode,
            )
            await asyncio.to_thread(hm.agent_store.create, config)

            if captured_files:
                try:
                    await asyncio.to_thread(
                        hm.agent_store.save_files,
                        agent_id,
                        captured_files,
                        None,
                        inspection_mode,
                        (room or {}).get("room_id"),
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to save agent files for %s: %s", agent_id, e,
                    )

            # -- Run pipeline --
            await hm.pipeline.run_query_agent_tracked(
                agent_id=agent_id,
                run_id=run_id,
                run_store=hm.run_store,
                artifact_store=hm.artifact_store,
                artifact_retention_seconds=hm.settings.artifact_retention_seconds,
                prompt=prompt,
                scope_agent_id=scope_agent_id,
                mediator_agent_id=mediator_agent_id,
                max_tokens=max_tokens,
                model=model,
                provider=provider,
                policy=policy,
                room_id=(room or {}).get("room_id"),
                room_manifest_hash=(room or {}).get("manifest_hash"),
                output_visibility=(room or {}).get(
                    "output_visibility", "owner_and_querier"
                ),
                allowed_llm_providers=(room or {}).get("allowed_llm_providers"),
                artifacts_enabled=bool((room or {}).get("allow_artifacts", True)),
                room_vault_items=room_vault_items or [],
                payer_tenant_id=payer_tenant_id,
                payer_token_id=payer_token_id,
                billable_role=billable_role,
                billing_provider=billing_provider,
                billing_model=billing_model,
                billing_hold_micro_usd=billing_hold_micro_usd,
            )

        except Exception as e:
            logger.error("Background build+run %s failed: %s", run_id, e)
            try:
                await asyncio.to_thread(
                    hm.run_store.update_status, run_id, "failed",
                    error=str(e)[:500],
                )
                await _settle_empty_billing(
                    hm,
                    run_id,
                    billing,
                    billable_role=billable_role,
                )
            except Exception:
                pass

    return app


class _LazyApp:
    """ASGI wrapper that delays Settings/.env loading until first request."""

    def __init__(self):
        self._app: FastAPI | None = None
        self._lock = threading.Lock()

    def _get_app(self) -> FastAPI:
        if self._app is None:
            with self._lock:
                if self._app is None:
                    self._app = create_app()
        return self._app

    async def __call__(self, scope, receive, send):
        await self._get_app()(scope, receive, send)


app = _LazyApp()


def main():
    import os
    import tempfile
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = Settings()

    # When enclave-terminated TLS is on, bootstrap attestation BEFORE
    # uvicorn.run() so we have the cert/key in hand before the socket
    # opens. The lifespan call becomes a no-op thanks to bootstrap's
    # idempotency guard.
    ssl_kwargs: dict = {}
    if os.environ.get("HIVEMIND_ENCLAVE_TLS"):
        from . import attestation as _att

        logger.info("HIVEMIND_ENCLAVE_TLS=1 — bootstrapping TLS before listen")
        _att.bootstrap()
        tls = _att.get_tls_material()
        if tls is None:
            logger.error(
                "Enclave TLS requested but derivation failed; falling back to HTTP. "
                "Check DSTACK_SIMULATOR_ENDPOINT / /var/run/dstack.sock."
            )
        else:
            cert_pem, key_pem = tls
            # uvicorn wants filesystem paths. tmpfs mounts are safe inside
            # the enclave; the cert/key are derived fresh every boot anyway.
            tdir = tempfile.mkdtemp(prefix="hivemind-tls-")
            cert_path = os.path.join(tdir, "cert.pem")
            key_path = os.path.join(tdir, "key.pem")
            with open(cert_path, "wb") as f:
                f.write(cert_pem)
            with open(key_path, "wb") as f:
                f.write(key_pem)
            os.chmod(key_path, 0o600)
            ssl_kwargs = {
                "ssl_certfile": cert_path,
                "ssl_keyfile": key_path,
            }
            logger.info(
                "TLS cert derived from dstack-KMS; "
                "fingerprint bound into REPORT_DATA v2"
            )

    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level="info",
        **ssl_kwargs,
    )


if __name__ == "__main__":
    main()
