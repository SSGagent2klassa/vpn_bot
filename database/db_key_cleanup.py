"""Atomic retention cleanup for expired VPN keys."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from .connection import get_db

logger = logging.getLogger(__name__)

__all__ = ["delete_expired_keys_older_than"]


def delete_expired_keys_older_than(
    retention_days: int,
) -> List[Dict[str, Any]]:
    """Delete keys expired for at least ``retention_days`` in one transaction."""
    if (
        isinstance(retention_days, bool)
        or not isinstance(retention_days, int)
        or retention_days <= 0
    ):
        raise ValueError("retention_days must be a positive integer")

    cutoff_modifier = f"-{retention_days} days"
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT
                vk.id,
                vk.user_id,
                vk.custom_name,
                vk.panel_email,
                vk.expires_at,
                u.telegram_id
            FROM vpn_keys vk
            JOIN users u ON u.id = vk.user_id
            WHERE vk.expires_at IS NOT NULL
              AND TRIM(CAST(vk.expires_at AS TEXT)) <> ''
              AND TRIM(CAST(vk.expires_at AS TEXT))
                  GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'
              AND datetime(vk.expires_at) IS NOT NULL
              AND datetime(vk.expires_at) <= datetime('now', ?)
            ORDER BY u.telegram_id ASC, vk.expires_at ASC, vk.id ASC
            """,
            (cutoff_modifier,),
        ).fetchall()
        deleted = [dict(row) for row in rows]
        if not deleted:
            return []

        key_ids = [int(row["id"]) for row in deleted]
        placeholders = ",".join("?" for _ in key_ids)
        conn.execute(
            f"UPDATE payments SET vpn_key_id = NULL "
            f"WHERE vpn_key_id IN ({placeholders})",
            key_ids,
        )
        conn.execute(
            f"DELETE FROM notification_log "
            f"WHERE vpn_key_id IN ({placeholders})",
            key_ids,
        )
        cursor = conn.execute(
            f"DELETE FROM vpn_keys WHERE id IN ({placeholders})",
            key_ids,
        )
        if cursor.rowcount != len(key_ids):
            raise RuntimeError(
                "Expired-key cleanup deleted an unexpected number of rows"
            )

    logger.info(
        "Deleted %s VPN keys expired for at least %s days",
        len(deleted),
        retention_days,
    )
    return deleted
