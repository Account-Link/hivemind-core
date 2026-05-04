"""Self-serve tenant signup routes."""

from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from typing import Any

from ..config import Settings
from ..tenants import TenantRegistry

logger = logging.getLogger(__name__)


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

        # Auto-redeem the configured starter credit code, if any. Best-
        # effort: a misconfigured/exhausted/expired code does not fail
        # signup. Done server-side so CLI signups (`hmctl signup`) and
        # website signups get the same balance, instead of the website
        # having an out-of-band redemption that hmctl users miss.
        starter_credit_micro_usd = 0
        starter_code = (settings.signup_starter_credit_code or "").strip()
        if starter_code:
            try:
                redemption = await asyncio.to_thread(
                    registry.redeem_credit_code,
                    starter_code,
                    result["tenant_id"],
                )
                credit = int(redemption.get("credit_micro_usd") or 0)
                if credit > 0:
                    await asyncio.to_thread(
                        registry.billing_grant_credit_micro,
                        result["tenant_id"],
                        credit,
                        note="signup starter credit",
                        actor="signup",
                        metadata={
                            "code_id": redemption.get("code_id"),
                            "redemption_id": redemption.get("redemption_id"),
                        },
                    )
                    starter_credit_micro_usd = credit
            except Exception:
                logger.warning(
                    "signup starter credit redemption failed for tenant %s",
                    result["tenant_id"],
                    exc_info=True,
                )

        balance = await asyncio.to_thread(
            registry.billing_balance_micro_usd,
            result["tenant_id"],
        )

        return {
            "tenant_id": result["tenant_id"],
            "name": result["name"],
            "api_key": result["api_key"],
            "starter_credit_micro_usd": starter_credit_micro_usd,
            "balance_micro_usd": int(balance or 0),
        }
