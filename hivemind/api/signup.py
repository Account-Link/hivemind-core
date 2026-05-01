"""Self-serve tenant signup routes."""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from typing import Any

from ..config import Settings
from ..tenants import TenantRegistry


class SignupRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: Any = None


def _registry(request: Request) -> TenantRegistry:
    return request.app.state.registry


def register_signup_routes(app: FastAPI, settings: Settings) -> None:
    """Register public self-serve signup routes."""

    @app.post("/v1/signup")
    async def signup(payload: SignupRequest, request: Request):
        if not settings.self_serve_signup_enabled:
            raise HTTPException(503, "self-serve signup is disabled")
        name = str(payload.name or "").strip()
        if not name:
            raise HTTPException(400, "'name' required")
        if any(
            key in (payload.model_extra or {})
            for key in ("credit_code", "invite_code", "signup_code", "code")
        ):
            raise HTTPException(
                400,
                "credit codes are redeemed after signup at "
                "/v1/billing/credit-codes/redeem",
            )
        registry = _registry(request)
        try:
            result = await asyncio.to_thread(
                registry.provision,
                name,
                allow_duplicate_name=True,
            )
        except RuntimeError as e:
            raise HTTPException(503, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))

        balance = await asyncio.to_thread(
            registry.billing_balance_micro_usd,
            result["tenant_id"],
        )

        return {
            "tenant_id": result["tenant_id"],
            "name": result["name"],
            "api_key": result["api_key"],
            "starter_credit_micro_usd": 0,
            "balance_micro_usd": int(balance or 0),
        }
