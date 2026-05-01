"""Credit-code operations for the tenant control plane."""

from __future__ import annotations

import secrets
import time
from typing import Any

from .tenant_keys import (
    hash_api_key,
    new_credit_code,
    new_credit_code_id,
    usd_to_micro_usd_nonnegative,
)


class CreditCodeRegistryMixin:
    """Admin-minted credit code storage and redemption."""

    def create_credit_code(
        self,
        *,
        credit_usd: Any = "0.00",
        max_redemptions: int = 1,
        expires_at: float | None = None,
        label: str = "",
    ) -> dict:
        """Create an admin-minted credit code for tenant recharge.

        Returns the plaintext ``code`` exactly once. Only the hash is stored.
        """
        credit = usd_to_micro_usd_nonnegative(credit_usd)
        uses = int(max_redemptions or 0)
        if uses <= 0:
            raise ValueError("max_redemptions must be positive")
        expiry = None if expires_at in (None, "") else float(expires_at)
        now = time.time()
        if expiry is not None and expiry <= now:
            raise ValueError("expires_at must be in the future")
        code_id = new_credit_code_id()
        code = new_credit_code()
        self._control_db.execute_commit(
            "INSERT INTO _credit_codes "
            "(code_id, code_hash, label, credit_micro_usd, "
            "max_redemptions, redeemed_count, created_at, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, 0, %s, %s)",
            [
                code_id,
                hash_api_key(code),
                (label or "").strip(),
                credit,
                uses,
                now,
                expiry,
            ],
        )
        return {
            "code_id": code_id,
            "code": code,
            "label": (label or "").strip(),
            "credit_micro_usd": credit,
            "max_redemptions": uses,
            "redeemed_count": 0,
            "created_at": now,
            "expires_at": expiry,
            "revoked_at": None,
        }

    def list_credit_codes(
        self,
        *,
        include_revoked: bool = False,
        limit: int = 100,
    ) -> list[dict]:
        where = "" if include_revoked else "WHERE revoked_at IS NULL"
        rows = self._control_db.execute(
            "SELECT code_id, label, credit_micro_usd, max_redemptions, "
            "redeemed_count, created_at, expires_at, revoked_at "
            f"FROM _credit_codes {where} "
            "ORDER BY created_at DESC LIMIT %s",
            [min(max(1, int(limit)), 500)],
        )
        now = time.time()
        out: list[dict] = []
        for row in rows:
            item = dict(row)
            remaining = max(
                0,
                int(item.get("max_redemptions") or 0)
                - int(item.get("redeemed_count") or 0),
            )
            expired = (
                item.get("expires_at") is not None
                and float(item["expires_at"]) <= now
            )
            item["remaining_redemptions"] = remaining
            item["expired"] = expired
            item["active"] = (
                item.get("revoked_at") is None
                and not expired
                and remaining > 0
            )
            out.append(item)
        return out

    def _credit_code_by_code(self, code: str) -> dict | None:
        clean = (code or "").strip()
        if not clean:
            return None
        rows = self._control_db.execute(
            "SELECT code_id, label, credit_micro_usd, max_redemptions, "
            "redeemed_count, created_at, expires_at, revoked_at "
            "FROM _credit_codes WHERE code_hash = %s",
            [hash_api_key(clean)],
        )
        return dict(rows[0]) if rows else None

    def _validate_credit_code(self, row: dict | None) -> dict:
        if row is None:
            raise ValueError("invalid credit code")
        if row.get("revoked_at") is not None:
            raise ValueError("credit code has been revoked")
        expires_at = row.get("expires_at")
        if expires_at is not None and float(expires_at) <= time.time():
            raise ValueError("credit code has expired")
        if int(row.get("redeemed_count") or 0) >= int(
            row.get("max_redemptions") or 0
        ):
            raise ValueError("credit code has no redemptions remaining")
        return row

    def preview_credit_code(self, code: str) -> dict:
        """Validate a credit code without consuming a redemption."""
        return self._validate_credit_code(self._credit_code_by_code(code))

    def redeem_credit_code(self, code: str, tenant_id: str) -> dict:
        """Consume one credit-code redemption for a tenant."""
        tenant = self.get_by_id(tenant_id)
        if tenant is None:
            raise KeyError(f"tenant '{tenant_id}' not found")
        clean = (code or "").strip()
        if not clean:
            raise ValueError("credit code required")
        row = self._validate_credit_code(self._credit_code_by_code(clean))
        rows = self._control_db.execute(
            "SELECT redemption_id FROM _credit_code_redemptions "
            "WHERE code_id = %s AND tenant_id = %s",
            [row["code_id"], tenant_id],
        )
        if rows:
            raise ValueError("credit code already redeemed by this tenant")
        now = time.time()
        rowcount = self._control_db.execute_commit(
            "UPDATE _credit_codes "
            "SET redeemed_count = redeemed_count + 1 "
            "WHERE code_id = %s "
            "AND revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > %s) "
            "AND redeemed_count < max_redemptions",
            [row["code_id"], now],
        )
        if not rowcount:
            self._validate_credit_code(self._credit_code_by_code(clean))
            raise ValueError("credit code has no redemptions remaining")
        redemption_id = "ccr_" + secrets.token_hex(12)
        try:
            self._control_db.execute_commit(
                "INSERT INTO _credit_code_redemptions "
                "(redemption_id, code_id, tenant_id, redeemed_at) "
                "VALUES (%s, %s, %s, %s)",
                [redemption_id, row["code_id"], tenant_id, now],
            )
        except Exception:
            self._control_db.execute_commit(
                "UPDATE _credit_codes "
                "SET redeemed_count = GREATEST(0, redeemed_count - 1) "
                "WHERE code_id = %s",
                [row["code_id"]],
            )
            raise
        row["redeemed_count"] = int(row.get("redeemed_count") or 0) + 1
        row["remaining_redemptions"] = max(
            0,
            int(row.get("max_redemptions") or 0)
            - int(row.get("redeemed_count") or 0),
        )
        row["redemption_id"] = redemption_id
        row["tenant_id"] = tenant_id
        row["redeemed_at"] = now
        return row

    def release_credit_code(self, code_id: str, tenant_id: str) -> None:
        """Undo a just-created redemption after downstream rollback."""
        deleted = self._control_db.execute_commit(
            "DELETE FROM _credit_code_redemptions "
            "WHERE code_id = %s AND tenant_id = %s",
            [code_id, tenant_id],
        )
        if deleted:
            self._control_db.execute_commit(
                "UPDATE _credit_codes "
                "SET redeemed_count = GREATEST(0, redeemed_count - %s) "
                "WHERE code_id = %s",
                [deleted, code_id],
            )

    def revoke_credit_code(self, code_id: str) -> bool:
        rowcount = self._control_db.execute_commit(
            "UPDATE _credit_codes SET revoked_at = %s "
            "WHERE code_id = %s AND revoked_at IS NULL",
            [time.time(), (code_id or "").strip()],
        )
        return bool(rowcount)
