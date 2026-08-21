"""Durable at-least-once delivery queue for extension completion handlers."""
from __future__ import annotations

import json
from typing import Any

from .connection import get_db


def ensure_extension_completion_job(
    order_id: str,
    *,
    key_id: int,
) -> dict[str, Any] | None:
    """Create the one durable completion job described by a PaymentIntent snapshot."""
    normalized_key_id = _positive_int(key_id, 'key_id')
    with get_db() as conn:
        payment = conn.execute(
            """
            SELECT origin_extension_id, origin_completion_handler,
                   origin_workflow_id, purpose, vpn_key_id
            FROM payments
            WHERE order_id = ? AND intent_version = 1
            """,
            (str(order_id),),
        ).fetchone()
        if (
            payment is None
            or str(payment['purpose'] or '') != 'key_purchase'
            or not payment['origin_extension_id']
            or not payment['origin_completion_handler']
            or not payment['origin_workflow_id']
        ):
            return None
        if int(payment['vpn_key_id'] or 0) != normalized_key_id:
            raise ValueError('completion key does not match its PaymentIntent')
        conn.execute(
            """
            INSERT OR IGNORE INTO extension_completion_jobs (
                extension_id, handler_name, workflow_id, order_id,
                key_id, stage, state, attempts, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 'key_configured', 'waiting', 0,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                str(payment['origin_extension_id']),
                str(payment['origin_completion_handler']),
                str(payment['origin_workflow_id']),
                str(order_id),
                normalized_key_id,
            ),
        )
        conn.execute(
            """
            UPDATE extension_completion_jobs
            SET key_id = COALESCE(key_id, ?), updated_at = CURRENT_TIMESTAMP
            WHERE extension_id = ? AND handler_name = ? AND workflow_id = ?
            """,
            (
                normalized_key_id,
                str(payment['origin_extension_id']),
                str(payment['origin_completion_handler']),
                str(payment['origin_workflow_id']),
            ),
        )
        row = conn.execute(
            """
            SELECT * FROM extension_completion_jobs
            WHERE extension_id = ? AND handler_name = ? AND workflow_id = ?
            """,
            (
                str(payment['origin_extension_id']),
                str(payment['origin_completion_handler']),
                str(payment['origin_workflow_id']),
            ),
        ).fetchone()
        if row is None:
            return None
        if (
            str(row['order_id']) != str(order_id)
            or int(row['key_id'] or 0) != normalized_key_id
        ):
            raise ValueError('completion workflow is already linked to another purchase')
        return dict(row)


def mark_extension_completion_ready(
    order_id: str,
    *,
    key_id: int,
) -> dict[str, Any] | None:
    """Make an existing job runnable once its key has a complete binding."""
    job = ensure_extension_completion_job(order_id, key_id=key_id)
    if job is None:
        return None
    with get_db() as conn:
        conn.execute(
            """
            UPDATE extension_completion_jobs
            SET state = 'ready', next_retry_at = NULL,
                lease_until = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND state = 'waiting'
              AND key_id = ?
              AND EXISTS (
                    SELECT 1
                    FROM payments p
                    WHERE p.order_id = extension_completion_jobs.order_id
                      AND p.status = 'paid'
                      AND p.fulfillment_status = 'completed'
                      AND p.vpn_key_id = extension_completion_jobs.key_id
              )
              AND EXISTS (
                    SELECT 1
                    FROM vpn_keys k
                    WHERE k.id = extension_completion_jobs.key_id
                      AND k.server_id IS NOT NULL
                      AND COALESCE(k.panel_email, '') <> ''
                      AND COALESCE(k.sub_id, '') <> ''
              )
            """,
            (int(job['id']), int(key_id)),
        )
        row = conn.execute(
            "SELECT * FROM extension_completion_jobs WHERE id = ?",
            (int(job['id']),),
        ).fetchone()
        return dict(row) if row is not None else None


def promote_configured_extension_completion_jobs(*, limit: int = 100) -> int:
    """Recover jobs left waiting after a crash following key configuration."""
    batch = max(1, min(int(limit), 500))
    with get_db() as conn:
        conn.execute(
            """
            UPDATE extension_completion_jobs
            SET state = 'degraded', next_retry_at = NULL, lease_until = NULL,
                last_error_code = 'key_deleted', updated_at = CURRENT_TIMESTAMP
            WHERE key_id IS NULL
              AND (
                    state IN ('waiting', 'ready', 'retry')
                    OR (
                        state = 'processing'
                        AND (lease_until IS NULL OR lease_until <= CURRENT_TIMESTAMP)
                    )
              )
            """
        )
        rows = conn.execute(
            """
            SELECT j.id
            FROM extension_completion_jobs j
            JOIN payments p
              ON p.order_id = j.order_id
             AND p.vpn_key_id = j.key_id
            JOIN vpn_keys k ON k.id = j.key_id
            WHERE j.state = 'waiting'
              AND p.status = 'paid'
              AND p.fulfillment_status = 'completed'
              AND k.server_id IS NOT NULL
              AND COALESCE(k.panel_email, '') <> ''
              AND COALESCE(k.sub_id, '') <> ''
            ORDER BY j.id
            LIMIT ?
            """,
            (batch,),
        ).fetchall()
        ids = [int(row['id']) for row in rows]
        if not ids:
            return 0
        placeholders = ','.join('?' for _ in ids)
        cursor = conn.execute(
            f"""
            UPDATE extension_completion_jobs
            SET state = 'ready', next_retry_at = NULL,
                lease_until = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE state = 'waiting'
              AND id IN ({placeholders})
              AND EXISTS (
                    SELECT 1
                    FROM payments p
                    WHERE p.order_id = extension_completion_jobs.order_id
                      AND p.status = 'paid'
                      AND p.fulfillment_status = 'completed'
                      AND p.vpn_key_id = extension_completion_jobs.key_id
              )
              AND EXISTS (
                    SELECT 1
                    FROM vpn_keys k
                    WHERE k.id = extension_completion_jobs.key_id
                      AND k.server_id IS NOT NULL
                      AND COALESCE(k.panel_email, '') <> ''
                      AND COALESCE(k.sub_id, '') <> ''
              )
            """,
            tuple(ids),
        )
        return int(cursor.rowcount)


def get_due_extension_completion_job_ids(*, limit: int = 100) -> list[int]:
    """Return bounded runnable ids; claiming remains a separate atomic operation."""
    batch = max(1, min(int(limit), 500))
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id
            FROM extension_completion_jobs
            WHERE (
                    (
                        state IN ('ready', 'retry')
                        AND (next_retry_at IS NULL OR next_retry_at <= CURRENT_TIMESTAMP)
                    )
                    OR (
                        state = 'processing'
                        AND (lease_until IS NULL OR lease_until <= CURRENT_TIMESTAMP)
                    )
                  )
              AND EXISTS (
                    SELECT 1
                    FROM payments p
                    WHERE p.order_id = extension_completion_jobs.order_id
                      AND p.status = 'paid'
                      AND p.fulfillment_status = 'completed'
                      AND p.vpn_key_id = extension_completion_jobs.key_id
              )
            ORDER BY id
            LIMIT ?
            """,
            (batch,),
        ).fetchall()
        return [int(row['id']) for row in rows]


def claim_extension_completion_job(
    job_id: int,
    *,
    lease_seconds: int = 120,
) -> dict[str, Any] | None:
    """Claim one due delivery and return its immutable PaymentIntent snapshot."""
    normalized_job_id = _positive_int(job_id, 'job_id')
    lease = max(30, min(int(lease_seconds), 60 * 60))
    with get_db() as conn:
        cursor = conn.execute(
            """
            UPDATE extension_completion_jobs
            SET state = 'processing', attempts = attempts + 1,
                lease_until = datetime('now', '+' || ? || ' seconds'),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND (
                    (state IN ('ready', 'retry')
                     AND (next_retry_at IS NULL OR next_retry_at <= CURRENT_TIMESTAMP))
                    OR
                    (state = 'processing'
                     AND (lease_until IS NULL OR lease_until <= CURRENT_TIMESTAMP))
                  )
              AND EXISTS (
                    SELECT 1
                    FROM payments p
                    WHERE p.order_id = extension_completion_jobs.order_id
                      AND p.status = 'paid'
                      AND p.fulfillment_status = 'completed'
                      AND p.vpn_key_id = extension_completion_jobs.key_id
              )
              AND EXISTS (
                    SELECT 1
                    FROM vpn_keys k
                    WHERE k.id = extension_completion_jobs.key_id
                      AND k.server_id IS NOT NULL
                      AND COALESCE(k.panel_email, '') <> ''
                      AND COALESCE(k.sub_id, '') <> ''
              )
            """,
            (lease, normalized_job_id),
        )
        if cursor.rowcount <= 0:
            return None
        row = conn.execute(
            """
            SELECT j.*, p.user_id, p.origin_context_version,
                   p.origin_context_json, u.telegram_id
            FROM extension_completion_jobs j
            JOIN payments p ON p.order_id = j.order_id
            JOIN users u ON u.id = p.user_id
            WHERE j.id = ?
            """,
            (normalized_job_id,),
        ).fetchone()
        return _decode_job(row)


def complete_extension_completion_job(job_id: int) -> bool:
    """Acknowledge successful delivery while retaining its audit row."""
    with get_db() as conn:
        cursor = conn.execute(
            """
            UPDATE extension_completion_jobs
            SET state = 'completed', lease_until = NULL, next_retry_at = NULL,
                last_error_code = NULL,
                completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND state = 'processing'
            """,
            (_positive_int(job_id, 'job_id'),),
        )
        return cursor.rowcount > 0


def retry_extension_completion_job(
    job_id: int,
    *,
    retry_after_seconds: int,
    error_code: str,
) -> bool:
    """Release a failed claim with a bounded retry schedule."""
    delay = max(1, min(int(retry_after_seconds), 24 * 60 * 60))
    with get_db() as conn:
        cursor = conn.execute(
            """
            UPDATE extension_completion_jobs
            SET state = 'retry', lease_until = NULL,
                next_retry_at = datetime('now', '+' || ? || ' seconds'),
                last_error_code = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND state = 'processing'
            """,
            (delay, _error_code(error_code), _positive_int(job_id, 'job_id')),
        )
        return cursor.rowcount > 0


def degrade_extension_completion_job(job_id: int, *, error_code: str) -> bool:
    """Stop automatic retries after an explicit or bounded terminal failure."""
    with get_db() as conn:
        cursor = conn.execute(
            """
            UPDATE extension_completion_jobs
            SET state = 'degraded', lease_until = NULL, next_retry_at = NULL,
                last_error_code = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND state = 'processing'
            """,
            (_error_code(error_code), _positive_int(job_id, 'job_id')),
        )
        return cursor.rowcount > 0


def get_extension_completion_job_for_order(order_id: str) -> dict[str, Any] | None:
    """Return one order's completion job for tests and operational summaries."""
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM extension_completion_jobs
            WHERE order_id = ? ORDER BY id DESC LIMIT 1
            """,
            (str(order_id),),
        ).fetchone()
        return dict(row) if row is not None else None


def _decode_job(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    try:
        payload = json.loads(str(result.get('origin_context_json') or '{}'))
    except json.JSONDecodeError as exc:
        raise ValueError('persisted completion origin payload is invalid') from exc
    if not isinstance(payload, dict):
        raise ValueError('persisted completion origin payload must be an object')
    result['origin_payload'] = payload
    return result


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f'{field} must be a positive integer')
    return value


def _error_code(value: Any) -> str:
    text = str(value or 'completion_failed').strip()
    return text[:1000] or 'completion_failed'


__all__ = [
    'claim_extension_completion_job',
    'complete_extension_completion_job',
    'degrade_extension_completion_job',
    'ensure_extension_completion_job',
    'get_due_extension_completion_job_ids',
    'get_extension_completion_job_for_order',
    'mark_extension_completion_ready',
    'promote_configured_extension_completion_jobs',
    'retry_extension_completion_job',
]
