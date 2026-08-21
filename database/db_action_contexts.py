"""Persistence for short-lived semantic action origin-context tokens."""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
from collections.abc import Mapping
from typing import Any

from .connection import get_db

DEFAULT_ACTION_CONTEXT_TTL_SECONDS = 20 * 60
_TOKEN_BYTES = 9
_TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]{12}$')


def create_semantic_action_context(
    *,
    user_id: int,
    action: str,
    owner_extension_id: str,
    schema_version: int,
    payload: Mapping[str, Any],
    workflow_id: str,
    completion_handler: str | None,
    ttl_seconds: int = DEFAULT_ACTION_CONTEXT_TTL_SECONDS,
) -> str:
    """Persist a validated context and return its compact callback token."""
    normalized = _normalized_values(
        user_id=user_id,
        action=action,
        owner_extension_id=owner_extension_id,
        schema_version=schema_version,
        payload=payload,
        workflow_id=workflow_id,
        completion_handler=completion_handler,
    )
    ttl = max(60, min(int(ttl_seconds), 24 * 60 * 60))
    with get_db() as conn:
        existing = conn.execute(
            """
            SELECT *, expires_at > CURRENT_TIMESTAMP AS is_unexpired
            FROM semantic_action_contexts
            WHERE owner_extension_id = ? AND workflow_id = ?
            """,
            (normalized['owner_extension_id'], normalized['workflow_id']),
        ).fetchone()
        if existing is not None:
            _assert_same_snapshot(existing, normalized)
            state = str(existing['state'] or '')
            if state == 'active':
                if bool(existing['is_unexpired']):
                    return str(existing['token'])
                return _reactivate_context(conn, normalized, ttl)
            order_id = str(existing['order_id'] or '')
            payment = (
                conn.execute(
                    "SELECT status FROM payments WHERE order_id = ?",
                    (order_id,),
                ).fetchone()
                if order_id
                else None
            )
            if not payment or str(payment['status'] or '') != 'canceled':
                raise ValueError('origin workflow has already been consumed')
            live_payment = conn.execute(
                """
                SELECT 1 FROM payments
                WHERE origin_extension_id = ? AND origin_workflow_id = ?
                  AND status <> 'canceled'
                LIMIT 1
                """,
                (
                    normalized['owner_extension_id'],
                    normalized['workflow_id'],
                ),
            ).fetchone()
            if live_payment is not None:
                raise ValueError('origin workflow already has a live payment intent')
            return _reactivate_context(conn, normalized, ttl)

        token = _new_unique_token(conn)
        conn.execute(
            """
            INSERT INTO semantic_action_contexts (
                token, user_id, owner_extension_id, action,
                schema_version, payload_json, workflow_id,
                completion_handler, state, expires_at, created_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, 'active',
                datetime('now', '+' || ? || ' seconds'), CURRENT_TIMESTAMP
            )
            """,
            (
                token,
                normalized['user_id'],
                normalized['owner_extension_id'],
                normalized['action'],
                normalized['schema_version'],
                normalized['payload_json'],
                normalized['workflow_id'],
                normalized['completion_handler'],
                ttl,
            ),
        )
        return token


def consume_semantic_action_context_with_conn(
    conn: sqlite3.Connection,
    token: str,
    *,
    user_id: int,
    action: str,
) -> dict[str, Any] | None:
    """Atomically consume an active owned token inside its caller's transaction."""
    normalized_token = _normalize_token(token)
    cursor = conn.execute(
        """
        UPDATE semantic_action_contexts
        SET state = 'consumed', consumed_at = CURRENT_TIMESTAMP
        WHERE token = ? AND user_id = ? AND action = ?
          AND state = 'active' AND expires_at > CURRENT_TIMESTAMP
        """,
        (normalized_token, int(user_id), str(action)),
    )
    if cursor.rowcount <= 0:
        return None
    row = conn.execute(
        """
        SELECT * FROM semantic_action_contexts
        WHERE token = ? AND user_id = ? AND action = ? AND state = 'consumed'
        """,
        (normalized_token, int(user_id), str(action)),
    ).fetchone()
    if row is None:
        raise RuntimeError('consumed origin context disappeared')
    return _decode_row(row)


def attach_semantic_action_context_order_with_conn(
    conn: sqlite3.Connection,
    token: str,
    *,
    order_id: str,
) -> bool:
    """Link a consumed transport token to the PaymentIntent created with it."""
    cursor = conn.execute(
        """
        UPDATE semantic_action_contexts
        SET order_id = ?
        WHERE token = ? AND state = 'consumed' AND order_id IS NULL
        """,
        (str(order_id), _normalize_token(token)),
    )
    return cursor.rowcount > 0


def expire_semantic_action_contexts() -> int:
    """Mark elapsed active contexts without deleting audit links."""
    with get_db() as conn:
        cursor = conn.execute(
            """
            UPDATE semantic_action_contexts
            SET state = 'expired'
            WHERE state = 'active' AND expires_at <= CURRENT_TIMESTAMP
            """
        )
        return int(cursor.rowcount)


def _normalized_values(**values: Any) -> dict[str, Any]:
    from bot.utils.action_origin_context import (
        encode_origin_payload,
        normalize_completion_handler_name,
        normalize_origin_workflow_id,
    )
    from bot.utils.action_policy import normalize_core_action
    from database.db_extensions import normalize_extension_id

    user_id = values['user_id']
    schema_version = values['schema_version']
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError('user_id must be a positive integer')
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 1
    ):
        raise ValueError('schema_version must be 1')
    return {
        'user_id': user_id,
        'action': normalize_core_action(values['action']),
        'owner_extension_id': normalize_extension_id(values['owner_extension_id']),
        'schema_version': schema_version,
        'payload_json': encode_origin_payload(values['payload']),
        'workflow_id': normalize_origin_workflow_id(values['workflow_id']),
        'completion_handler': normalize_completion_handler_name(
            values['completion_handler'],
            optional=True,
        ),
    }


def _assert_same_snapshot(row: sqlite3.Row, normalized: Mapping[str, Any]) -> None:
    fields = (
        'user_id',
        'action',
        'owner_extension_id',
        'schema_version',
        'payload_json',
        'completion_handler',
    )
    for field in fields:
        existing = row[field]
        expected = normalized[field]
        if field in {'user_id', 'schema_version'}:
            existing = int(existing)
        if existing != expected:
            raise ValueError('origin workflow snapshot does not match its persisted context')


def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(str(row['payload_json'] or '{}'))
    except json.JSONDecodeError as exc:
        raise ValueError('persisted origin payload is invalid') from exc
    if not isinstance(payload, dict):
        raise ValueError('persisted origin payload must be an object')
    return {
        'owner_extension_id': str(row['owner_extension_id']),
        'schema_version': int(row['schema_version']),
        'payload': payload,
        'workflow_id': str(row['workflow_id']),
        'completion_handler': (
            str(row['completion_handler']) if row['completion_handler'] else None
        ),
    }


def _new_unique_token(conn: sqlite3.Connection) -> str:
    for _ in range(8):
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        row = conn.execute(
            "SELECT 1 FROM semantic_action_contexts WHERE token = ?",
            (token,),
        ).fetchone()
        if row is None:
            return token
    raise RuntimeError('could not allocate a unique semantic action context token')


def _reactivate_context(
    conn: sqlite3.Connection,
    normalized: Mapping[str, Any],
    ttl_seconds: int,
) -> str:
    token = _new_unique_token(conn)
    conn.execute(
        """
        UPDATE semantic_action_contexts
        SET token = ?, state = 'active', order_id = NULL,
            expires_at = datetime('now', '+' || ? || ' seconds'),
            consumed_at = NULL
        WHERE owner_extension_id = ? AND workflow_id = ?
        """,
        (
            token,
            ttl_seconds,
            normalized['owner_extension_id'],
            normalized['workflow_id'],
        ),
    )
    return token


def _normalize_token(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError('action context token must be a string')
    token = value.strip()
    if not _TOKEN_RE.fullmatch(token):
        raise ValueError('action context token is invalid')
    return token


__all__ = [
    'DEFAULT_ACTION_CONTEXT_TTL_SECONDS',
    'attach_semantic_action_context_order_with_conn',
    'consume_semantic_action_context_with_conn',
    'create_semantic_action_context',
    'expire_semantic_action_contexts',
]
