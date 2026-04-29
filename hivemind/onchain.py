"""On-chain governance reads for the HivemindAppAuth contract.

Feedling's third binding, ported. Resolves a compose_hash against the
`isAppAllowed(bytes32)` view on the on-chain registry. The CLI uses
this to:

* **Auto-accept** any compose hash the contract owner has approved
  (no interactive y/N prompt, no TOFU).
* **Hard-abort** any compose hash the contract owner has revoked.

Reads go over plain HTTPS JSON-RPC — no web3.py dependency. A bad/stale
contract address, malformed RPC URL, or network error returns
``None`` ("unknown") so the caller can fall back to the CLI's
TOFU/change-prompt path. Never fail-open; unknown is not approved.

Selector: ``isAppAllowed(bytes32) -> bool`` = ``0x90144031``.
"""

from __future__ import annotations

import json

import httpx

# keccak256("isAppAllowed(bytes32)")[:4], without 0x prefix.
_IS_APP_ALLOWED_SELECTOR = "90144031"

# keccak256("releases(bytes32)")[:4] — auto-getter for the public mapping
# `mapping(bytes32 => ReleaseEntry) public releases`. Returns the tuple
# (bool approved, uint64 approvedAt, uint64 revokedAt, string gitCommit,
#  string composeYamlURI). Used to surface the source-of-record for an
# approved compose_hash so the CLI can print "registered from <git_sha>:
# <compose_uri>" alongside the trust check.
_RELEASES_SELECTOR = "f491a84c"

# Sepolia testnet chain id.
ETH_SEPOLIA_CHAIN_ID = 11155111
ETHERSCAN_SEPOLIA = "https://sepolia.etherscan.io"


def _strip_0x(s: str) -> str:
    return s[2:] if s.startswith(("0x", "0X")) else s


def _pad32(hex_str: str) -> str:
    """Left-pad a hex string to 32 bytes / 64 hex chars. No 0x prefix."""
    h = _strip_0x(hex_str).lower()
    if len(h) > 64:
        raise ValueError(f"too long for bytes32: {len(h)} hex chars")
    return h.rjust(64, "0")


def _encode_call(compose_hash: str) -> str:
    return _IS_APP_ALLOWED_SELECTOR + _pad32(compose_hash)


def _decode_bool(raw: str) -> bool:
    """ABI-decode a ``bool`` return value from ``eth_call``."""
    h = _strip_0x(raw)
    # Pad to 64 hex chars in case the RPC trims leading zeros.
    return int(h.rjust(64, "0"), 16) == 1


def is_app_allowed(
    rpc_url: str,
    contract: str,
    compose_hash: str,
    *,
    timeout: float = 5.0,
) -> bool | None:
    """Return True/False if the contract answers; None on any error.

    Returning None (rather than raising) lets ``_require_trust`` fall
    back to the local TOFU/change-prompt path when the RPC is
    unreachable or the contract is misconfigured. The CLI UI surfaces
    the distinction to the user.
    """
    if not rpc_url or not contract or not compose_hash:
        return None
    data = "0x" + _encode_call(compose_hash)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [
            {"to": contract, "data": data},
            "latest",
        ],
    }
    try:
        resp = httpx.post(
            rpc_url,
            json=payload,
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )
    except (httpx.HTTPError, OSError):
        return None
    if resp.status_code != 200:
        return None
    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError):
        return None
    result = body.get("result")
    if not isinstance(result, str) or not result.startswith(("0x", "0X")):
        return None
    try:
        return _decode_bool(result)
    except (ValueError, TypeError):
        return None


def _decode_release(hex_data: str) -> dict | None:
    """ABI-decode the ``releases(bytes32)`` return tuple.

    Layout (head 5 * 32 bytes, then tail with two strings):
      [0x00]  bool approved          (32-byte uint, 0/1)
      [0x20]  uint64 approvedAt      (32-byte big-endian)
      [0x40]  uint64 revokedAt       (32-byte big-endian)
      [0x60]  offset to gitCommit    (relative to start of returndata)
      [0x80]  offset to composeURI
      [off1]  uint256 length || bytes (zero-padded to 32-byte multiple)
      [off2]  uint256 length || bytes

    Returns None on any decode failure so callers fall back to a
    cheap on-chain ``isAppAllowed`` boolean check.
    """
    h = _strip_0x(hex_data).lower()
    try:
        b = bytes.fromhex(h)
    except ValueError:
        return None
    if len(b) < 5 * 32:
        return None
    try:
        approved = int.from_bytes(b[0:32], "big") == 1
        approved_at = int.from_bytes(b[32:64], "big")
        revoked_at = int.from_bytes(b[64:96], "big")
        off1 = int.from_bytes(b[96:128], "big")
        off2 = int.from_bytes(b[128:160], "big")
        if off1 + 32 > len(b) or off2 + 32 > len(b):
            return None
        len1 = int.from_bytes(b[off1 : off1 + 32], "big")
        len2 = int.from_bytes(b[off2 : off2 + 32], "big")
        if off1 + 32 + len1 > len(b) or off2 + 32 + len2 > len(b):
            return None
        git_commit = b[off1 + 32 : off1 + 32 + len1].decode("utf-8", "replace")
        compose_uri = b[off2 + 32 : off2 + 32 + len2].decode("utf-8", "replace")
    except (ValueError, IndexError, UnicodeDecodeError):
        return None
    return {
        "approved": approved,
        "approved_at": approved_at,
        "revoked_at": revoked_at,
        "git_commit": git_commit,
        "compose_uri": compose_uri,
    }


def release_metadata(
    rpc_url: str,
    contract: str,
    compose_hash: str,
    *,
    timeout: float = 5.0,
) -> dict | None:
    """Read source metadata for ``compose_hash`` from the on-chain registry.

    Returns ``{"approved", "approved_at", "revoked_at", "git_commit",
    "compose_uri"}`` on success, ``None`` on any RPC / decode failure.
    Never raises — the caller treats ``None`` as "registry didn't tell us
    where the source lives" and skips the source-line UX.
    """
    if not rpc_url or not contract or not compose_hash:
        return None
    data = "0x" + _RELEASES_SELECTOR + _pad32(compose_hash)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": contract, "data": data}, "latest"],
    }
    try:
        resp = httpx.post(
            rpc_url,
            json=payload,
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )
    except (httpx.HTTPError, OSError):
        return None
    if resp.status_code != 200:
        return None
    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError):
        return None
    result = body.get("result")
    if not isinstance(result, str):
        return None
    return _decode_release(result)


def explorer_link(contract: str, chain_id: int = ETH_SEPOLIA_CHAIN_ID) -> str:
    """Return an Etherscan URL for ``contract`` on ``chain_id``."""
    if chain_id == ETH_SEPOLIA_CHAIN_ID:
        return f"{ETHERSCAN_SEPOLIA}/address/{contract}"
    return ""


__all__ = [
    "ETH_SEPOLIA_CHAIN_ID",
    "ETHERSCAN_SEPOLIA",
    "is_app_allowed",
    "release_metadata",
    "explorer_link",
]
