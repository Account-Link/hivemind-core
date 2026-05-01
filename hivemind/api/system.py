"""System, attestation, health, and schema routes."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import httpx
from fastapi import Depends, FastAPI

from ..config import Settings
from ..models import HealthResponse
from ..tenants import Caller
from ..version import APP_VERSION


def register_system_routes(
    app: FastAPI,
    settings: Settings,
    check_admin: Callable,
    requires_role: Callable[..., Callable],
) -> None:
    """Register small system and introspection endpoints."""

    @app.get("/v1/healthz")
    async def healthz():
        return {"status": "ok", "version": APP_VERSION}

    @app.get("/v1/admin/llm-probe", dependencies=[Depends(check_admin)])
    async def llm_probe(provider: str = "", model: str = ""):
        """Admin-only: probe LLM provider connectivity from inside the CVM."""
        prov_key = (provider or "").strip().lower() or "openrouter"
        if prov_key == "tinfoil":
            base_url = settings.tinfoil_base_url
            api_key = settings.tinfoil_api_key
        elif prov_key == "openrouter":
            base_url = settings.llm_base_url
            api_key = settings.llm_api_key
        else:
            return {
                "error": (
                    f"unknown provider {provider!r}, expected "
                    "'openrouter' or 'tinfoil'"
                )
            }

        chosen_model = (model or "").strip() or settings.llm_model
        out: dict = {
            "provider": prov_key,
            "base_url": base_url,
            "model": chosen_model,
            "api_key_configured": bool(api_key),
            "timeout_seconds": settings.llm_timeout_seconds,
        }
        if not api_key:
            out["error"] = f"{prov_key} api_key not configured on server"
            return out

        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0),
            ) as client:
                r = await client.post(
                    f"{base_url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": chosen_model,
                        "messages": [{"role": "user", "content": "reply OK"}],
                        "max_tokens": 5,
                    },
                )
            out["status_code"] = r.status_code
            out["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
            out["body_head"] = r.text[:200]
        except Exception as e:
            out["error_class"] = type(e).__name__
            out["error"] = str(e)[:300]
            out["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        return out

    @app.get("/v1/attestation")
    async def attestation_endpoint():
        from .. import attestation as _att

        return _att.get_bundle()

    @app.get("/v1/admin/schema")
    async def get_schema(
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        schema = await asyncio.to_thread(caller.hive.db.get_schema)
        return {"schema": schema}

    @app.get("/v1/health", response_model=HealthResponse)
    async def health(
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        return await asyncio.to_thread(caller.hive.health)
