"""Tenant registry key and billing amount helpers."""

from __future__ import annotations

import hashlib
import secrets
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


TENANT_ID_PREFIX = "t_"
API_KEY_PREFIX = "hmk_"
CREDIT_CODE_ID_PREFIX = "cc_"
CREDIT_CODE_PREFIX = "hmcc_"

# Capability-token prefix. Owner tokens stay on hmk_ for backward compat;
# the prefix lets the resolver pick the right table without trying both
# lookups on every request.
QUERY_TOKEN_PREFIX = "hmq_"  # query-only: submit prompts via the active scope agent
SHARE_TOKEN_PREFIX = "hms_"  # stable per-room share link; anyone with it can ask

MICRO_USD = Decimal("1000000")
TOKENS_PER_MTOK = 1_000_000


def new_tenant_id() -> str:
    return TENANT_ID_PREFIX + secrets.token_hex(6)


def new_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def new_credit_code_id() -> str:
    return CREDIT_CODE_ID_PREFIX + secrets.token_hex(8)


def new_credit_code() -> str:
    return CREDIT_CODE_PREFIX + secrets.token_urlsafe(32)


def new_capability_token(prefix: str) -> str:
    """Mint a fresh capability token with the given prefix."""
    return prefix + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    # SHA-256 is fine here: Hivemind keys have >=256 bits of entropy, so
    # brute force is infeasible even without slow hashing.
    return hashlib.sha256(key.encode()).hexdigest()


def token_id(token_hash_hex: str) -> str:
    """Short, user-visible id for a hashed token."""
    return token_hash_hex[:12]


def usd_to_micro_usd(value: Any) -> int:
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError) as e:
        raise ValueError(f"invalid USD amount: {value!r}") from e
    if dec <= 0:
        raise ValueError("USD amount must be positive")
    return int((dec * MICRO_USD).to_integral_value(rounding=ROUND_HALF_UP))


def usd_to_micro_usd_nonnegative(value: Any) -> int:
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError) as e:
        raise ValueError(f"invalid USD amount: {value!r}") from e
    if dec < 0:
        raise ValueError("USD amount must be non-negative")
    return int((dec * MICRO_USD).to_integral_value(rounding=ROUND_HALF_UP))


def usd_per_mtok_to_micro(value: Any) -> int:
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError) as e:
        raise ValueError(f"invalid price amount: {value!r}") from e
    if dec < 0:
        raise ValueError("price must be non-negative")
    return int((dec * MICRO_USD).to_integral_value(rounding=ROUND_HALF_UP))


def charge_for_tokens(
    prompt_tokens: int,
    completion_tokens: int,
    prompt_microusd_per_mtok: int,
    completion_microusd_per_mtok: int,
) -> int:
    prompt = max(0, int(prompt_tokens or 0))
    completion = max(0, int(completion_tokens or 0))
    numerator = (
        prompt * max(0, int(prompt_microusd_per_mtok or 0))
        + completion * max(0, int(completion_microusd_per_mtok or 0))
    )
    if numerator <= 0:
        return 0
    return (numerator + TOKENS_PER_MTOK - 1) // TOKENS_PER_MTOK
