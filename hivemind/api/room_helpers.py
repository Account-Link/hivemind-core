"""Shared helpers for room-bound API routes and runs."""

from __future__ import annotations

import asyncio
from urllib.parse import quote

from fastapi import HTTPException, Request

from ..models import QueryRequest
from ..rooms import (
    RoomTrustUpdateRequest,
    inspection_mode_from_visibility,
    verify_room_envelope,
)
from ..tenants import Caller


async def load_room_for_caller(
    caller: Caller,
    room_id: str | None,
) -> dict:
    rid = (room_id or "").strip()
    if caller.role == "query":
        bound = (caller.constraints.get("room_id") or "").strip()
        if not bound:
            raise HTTPException(400, "query token is not bound to a room")
        if rid and rid != bound:
            raise HTTPException(403, "query token is bound to a different room")
        rid = bound
    if not rid:
        raise HTTPException(400, "room_id is required")
    room = await asyncio.to_thread(caller.hive.room_store.get, rid)
    if not room:
        raise HTTPException(404, f"room '{rid}' not found")
    ok, reason = verify_room_envelope(room.get("envelope") or {})
    if not ok:
        raise HTTPException(
            409,
            f"room '{rid}' has an invalid signed manifest: {reason}",
        )
    if room.get("revoked_at") is not None:
        raise HTTPException(403, f"room '{rid}' is revoked")
    return room


def validate_room_provider(req_provider: str | None, room: dict) -> None:
    allowed = [p.strip().lower() for p in room.get("allowed_llm_providers") or []]
    requested = (req_provider or "").strip().lower()
    if not allowed:
        if requested:
            raise HTTPException(
                400,
                "this room disallows external LLM egress; omit provider",
            )
        return
    selected = requested or allowed[0]
    if selected not in allowed:
        raise HTTPException(
            400,
            f"provider '{selected}' is not allowed by this room "
            f"(allowed_llm_providers={allowed})",
        )


def room_query_inspection_mode(room: dict) -> str:
    manifest = room.get("manifest") or {}
    query = manifest.get("query") or {}
    return inspection_mode_from_visibility(query.get("visibility"))


def room_prompt_for_run(room: dict | None, prompt: str) -> str | None:
    if not room:
        return None
    manifest = room.get("manifest") or {}
    query = manifest.get("query") or {}
    if query.get("visibility") != "inspectable":
        return None
    return prompt


def room_wrap_id(caller: Caller) -> str:
    if caller.role == "owner":
        return "owner"
    token_id = (caller.token_id or "").strip()
    if not token_id:
        raise HTTPException(500, "query caller is missing token_id")
    return f"query:{token_id}"


def room_link(request: Request, room_id: str, token: str, pubkey_b64: str) -> str:
    base = str(request.base_url).rstrip("/")
    host = request.url.netloc or "service"
    return (
        f"hmroom://{host}/{room_id}"
        f"?service={quote(base, safe='')}"
        f"&token={quote(token, safe='')}"
        f"&owner_pubkey={quote(pubkey_b64, safe='')}"
    )


def apply_room_to_query_request(
    req: QueryRequest,
    room: dict,
) -> QueryRequest:
    manifest = room.get("manifest") or {}
    query = manifest.get("query") or {}
    mediator = manifest.get("mediator")
    mode = query.get("mode") or room.get("query_mode")
    fixed_query_agent_id = (
        query.get("agent_id") or room.get("fixed_query_agent_id") or ""
    ).strip()
    if mode == "fixed":
        if not fixed_query_agent_id:
            raise HTTPException(500, "room fixed query agent is missing")
        query_agent_id = fixed_query_agent_id
    else:
        query_agent_id = (req.query_agent_id or "").strip()
        if not query_agent_id:
            raise HTTPException(
                400,
                "room requires a query_agent_id; upload a query agent "
                "or use a room with query.mode='fixed'",
            )
    room_policy = room.get("policy") or ""
    requested_policy = (req.policy or "").strip()
    if requested_policy and requested_policy != room_policy:
        raise HTTPException(
            400,
            "room policy is fixed by the signed room manifest; "
            "caller-supplied policy cannot override it",
        )
    mediator_agent_id = req.mediator_agent_id
    if isinstance(mediator, dict):
        fixed_mediator_agent_id = (mediator.get("agent_id") or "").strip()
        requested_mediator_agent_id = (req.mediator_agent_id or "").strip()
        if fixed_mediator_agent_id:
            if (
                requested_mediator_agent_id
                and requested_mediator_agent_id != fixed_mediator_agent_id
            ):
                raise HTTPException(
                    400,
                    "room mediator agent is fixed by the signed room "
                    "manifest; caller-supplied mediator cannot override it",
                )
            mediator_agent_id = fixed_mediator_agent_id
        elif requested_mediator_agent_id:
            raise HTTPException(
                400,
                "room manifest does not allow a mediator-agent override",
            )
    validate_room_provider(req.provider, room)
    return req.model_copy(
        update={
            "room_id": room["room_id"],
            "scope_agent_id": room["scope_agent_id"],
            "query_agent_id": query_agent_id,
            "mediator_agent_id": mediator_agent_id,
            "policy": room_policy,
        }
    )


def live_compose_hash() -> str:
    from .. import attestation as _att

    bundle = _att.get_bundle()
    if not bundle.get("ready"):
        return ""
    return ((bundle.get("attestation") or {}).get("compose_hash") or "").lower()


def compose_trust_from_update(
    current: dict,
    req: RoomTrustUpdateRequest,
) -> dict:
    mode = req.mode or current.get("mode") or "operator_updates"
    if req.allowed_composes is None:
        allowed = [
            str(c).strip().lower()
            for c in (current.get("allowed_composes") or [])
            if str(c).strip()
        ]
    else:
        allowed = [
            str(c).strip().lower()
            for c in req.allowed_composes
            if str(c).strip()
        ]
    if req.append_live:
        live = live_compose_hash()
        if not live:
            raise HTTPException(400, "live compose_hash is not available")
        if live not in allowed:
            allowed.append(live)
    if mode in {"pinned", "owner_approved"} and not allowed:
        raise HTTPException(
            400,
            f"trust.mode='{mode}' requires allowed_composes or append_live=true",
        )
    return {"mode": mode, "allowed_composes": allowed}
