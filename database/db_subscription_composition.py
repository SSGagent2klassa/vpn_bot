"""Durable desired state for composing one key subscription into another."""

from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
from typing import Any, Iterable, Optional

from .connection import get_db

logger = logging.getLogger(__name__)

_SOURCE_NAMESPACE_RE = re.compile(r'^[a-z][a-z0-9_.:-]{0,127}$')
__all__ = [
    'claim_subscription_composition_sync',
    'create_subscription_binding',
    'delete_subscription_binding',
    'enqueue_subscription_composition_sync',
    'enqueue_subscription_composition_drift',
    'get_subscription_binding',
    'get_subscription_binding_by_pair',
    'get_subscription_composition_sync_state',
    'get_subscription_host_candidates',
    'list_component_subscription_bindings',
    'list_due_subscription_composition_hosts',
    'list_host_subscription_bindings',
    'list_owner_subscription_bindings',
    'list_subscription_composition_sync_states',
    'list_subscription_keys_for_owner',
    'list_subscription_bindings',
    'mark_subscription_composition_sync_blocked',
    'mark_subscription_composition_sync_partial',
    'mark_subscription_composition_sync_result',
    'mark_subscription_composition_sync_retry',
    'normalize_subscription_source_namespace',
    'normalize_subscription_source_reference',
    'release_subscription_composition_sync_lease',
    'renew_subscription_composition_sync_lease',
    'subscription_binding_path_exists',
    'unbind_subscription_binding',
]


def normalize_subscription_source_namespace(value: Any) -> str:
    """Returns a canonical audit namespace for a binding origin."""
    if not isinstance(value, str):
        raise ValueError('source_namespace must be a string')
    namespace = value.strip().casefold()
    if not _SOURCE_NAMESPACE_RE.fullmatch(namespace):
        raise ValueError(
            'source_namespace must start with a letter and contain only '
            'letters, digits, dots, underscores, colons or hyphens'
        )
    return namespace


def normalize_subscription_source_reference(value: Any) -> Optional[str]:
    """Validates an optional caller-owned idempotency/audit reference."""
    return _normalize_source_reference(value)


def create_subscription_binding(
    *,
    host_key_id: int,
    component_key_id: int,
    source_namespace: str,
    source_reference: Optional[str] = None,
    exclusive_component: bool = False,
) -> dict[str, Any]:
    """Creates one exact desired relation or returns its existing row.

    Ownership and graph validation run in the same serialized transaction as
    insertion. This keeps the repository invariant intact for every caller,
    including callers that bypass the higher-level service.
    """
    host_id = _positive_int(host_key_id, field='host_key_id')
    component_id = _positive_int(component_key_id, field='component_key_id')
    namespace = normalize_subscription_source_namespace(source_namespace)
    reference = _normalize_source_reference(source_reference)
    if not isinstance(exclusive_component, bool):
        raise ValueError('exclusive_component must be bool')
    if host_id == component_id:
        raise ValueError('A key cannot contain its own subscription')

    with get_db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        key_rows = conn.execute(
            """
            SELECT id, user_id
            FROM vpn_keys
            WHERE id IN (?, ?)
            """,
            (host_id, component_id),
        ).fetchall()
        keys = {int(row['id']): row for row in key_rows}
        if host_id not in keys:
            raise ValueError('host_key_id does not exist')
        if component_id not in keys:
            raise ValueError('component_key_id does not exist')
        if int(keys[host_id]['user_id']) != int(keys[component_id]['user_id']):
            raise ValueError('Subscription binding keys must have the same owner')

        existing = _get_binding_by_pair(conn, host_id, component_id)
        if existing is not None:
            result = _binding_result(existing)
            result['created'] = False
            return result

        if exclusive_component:
            component_binding = conn.execute(
                """
                SELECT 1
                FROM subscription_bindings
                WHERE component_key_id = ?
                LIMIT 1
                """,
                (component_id,),
            ).fetchone()
            if component_binding is not None:
                raise ValueError('Component key already has a subscription binding')

        if _binding_path_exists(
            conn,
            start_key_id=component_id,
            target_key_id=host_id,
        ):
            raise ValueError('Subscription binding would create a cycle')

        binding_id: Optional[int] = None
        random_part = ''
        for _ in range(4):
            random_part = secrets.token_urlsafe(18)
            provisional_token = f'pending:{random_part}'
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO subscription_bindings (
                        host_key_id, component_key_id, source_namespace,
                        source_reference, managed_token
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        host_id,
                        component_id,
                        namespace,
                        reference,
                        provisional_token,
                    ),
                )
            except sqlite3.IntegrityError:
                concurrent = _get_binding_by_pair(conn, host_id, component_id)
                if concurrent is not None:
                    result = _binding_result(concurrent)
                    result['created'] = False
                    return result
                continue
            binding_id = int(cursor.lastrowid)
            break
        if binding_id is None:
            raise RuntimeError('Could not allocate a unique subscription binding token')

        managed_token = f'subscription-binding:{binding_id}:{random_part}'
        conn.execute(
            """
            UPDATE subscription_bindings
            SET managed_token = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (managed_token, binding_id),
        )
        row = conn.execute(
            'SELECT * FROM subscription_bindings WHERE id = ?',
            (binding_id,),
        ).fetchone()
        result = _binding_result(row)
        result['created'] = True
        return result


def get_subscription_binding(binding_id: int) -> Optional[dict[str, Any]]:
    """Returns one binding by its internal ID."""
    normalized_id = _positive_int(binding_id, field='binding_id')
    with get_db() as conn:
        row = conn.execute(
            'SELECT * FROM subscription_bindings WHERE id = ?',
            (normalized_id,),
        ).fetchone()
        return _binding_result(row) if row is not None else None


def get_subscription_binding_by_pair(
    *,
    host_key_id: int,
    component_key_id: int,
) -> Optional[dict[str, Any]]:
    """Returns the globally unique exact host/component relation."""
    host_id = _positive_int(host_key_id, field='host_key_id')
    component_id = _positive_int(component_key_id, field='component_key_id')
    with get_db() as conn:
        row = _get_binding_by_pair(conn, host_id, component_id)
        return _binding_result(row) if row is not None else None


def list_subscription_bindings(
    *,
    host_key_id: Optional[int] = None,
    component_key_id: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Lists bindings with optional exact host and component filters."""
    conditions: list[str] = []
    params: list[int] = []
    if host_key_id is not None:
        conditions.append('host_key_id = ?')
        params.append(_positive_int(host_key_id, field='host_key_id'))
    if component_key_id is not None:
        conditions.append('component_key_id = ?')
        params.append(_positive_int(component_key_id, field='component_key_id'))
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ''
    with get_db() as conn:
        rows = conn.execute(
            f'SELECT * FROM subscription_bindings{where} ORDER BY id',
            tuple(params),
        ).fetchall()
        return [_binding_result(row) for row in rows]


def list_host_subscription_bindings(*, host_key_id: int) -> list[dict[str, Any]]:
    """Lists desired component subscriptions for one host key."""
    return list_subscription_bindings(host_key_id=host_key_id)


def list_component_subscription_bindings(
    *,
    component_key_id: int,
) -> list[dict[str, Any]]:
    """Lists every host currently containing one component key."""
    return list_subscription_bindings(component_key_id=component_key_id)


def list_owner_subscription_bindings(
    *,
    owner_user_id: int,
    key_id: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Lists raw binding rows after enforcing ownership on both endpoints."""
    owner_id = _positive_int(owner_user_id, field='owner_user_id')
    normalized_key_id = (
        None if key_id is None else _positive_int(key_id, field='key_id')
    )
    key_filter = ''
    params: list[int] = [owner_id, owner_id]
    if normalized_key_id is not None:
        key_filter = ' AND (b.host_key_id = ? OR b.component_key_id = ?)'
        params.extend((normalized_key_id, normalized_key_id))
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT b.*, sync.state AS sync_state
            FROM subscription_bindings b
            JOIN vpn_keys host ON host.id = b.host_key_id
            JOIN vpn_keys component ON component.id = b.component_key_id
            LEFT JOIN subscription_composition_sync sync
                ON sync.host_key_id = b.host_key_id
            WHERE host.user_id = ? AND component.user_id = ?
            {key_filter}
            ORDER BY b.id
            """,
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]


def list_subscription_keys_for_owner(
    *,
    owner_user_id: int,
) -> list[dict[str, Any]]:
    """Returns non-secret key metadata used by the controlled facade."""
    owner_id = _positive_int(owner_user_id, field='owner_user_id')
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                vk.id, vk.user_id, vk.tariff_id, vk.custom_name,
                vk.expires_at, vk.traffic_used, vk.traffic_limit,
                vk.server_id,
                t.name AS tariff_name, t.group_id AS tariff_group_id,
                s.name AS server_name, s.is_active AS server_active,
                s.panel_version,
                CASE
                    WHEN vk.server_id IS NOT NULL
                     AND vk.panel_email IS NOT NULL
                     AND vk.sub_id IS NOT NULL
                    THEN 1 ELSE 0
                END AS is_configured
            FROM vpn_keys vk
            LEFT JOIN tariffs t ON t.id = vk.tariff_id
            LEFT JOIN servers s ON s.id = vk.server_id
            WHERE vk.user_id = ?
            ORDER BY vk.id
            """,
            (owner_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def delete_subscription_binding(
    *,
    host_key_id: int,
    component_key_id: int,
    source_namespace: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Deletes one exact desired relation and returns its former row.

    Supplying source_namespace makes the delete origin-safe: a caller cannot
    remove a relation first established by another namespace.
    """
    host_id = _positive_int(host_key_id, field='host_key_id')
    component_id = _positive_int(component_key_id, field='component_key_id')
    namespace = (
        None
        if source_namespace is None
        else normalize_subscription_source_namespace(source_namespace)
    )
    with get_db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = _get_binding_by_pair(conn, host_id, component_id)
        if row is None:
            return None
        if namespace is not None and str(row['source_namespace']) != namespace:
            return None
        conn.execute(
            'DELETE FROM subscription_bindings WHERE id = ?',
            (int(row['id']),),
        )
        return _binding_result(row)


def unbind_subscription_binding(
    *,
    host_key_id: int,
    component_key_id: int,
    source_namespace: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Alias with domain terminology for deleting an exact binding."""
    return delete_subscription_binding(
        host_key_id=host_key_id,
        component_key_id=component_key_id,
        source_namespace=source_namespace,
    )


def subscription_binding_path_exists(
    *,
    start_key_id: int,
    target_key_id: int,
) -> bool:
    """Returns whether the directed binding graph reaches target from start."""
    start_id = _positive_int(start_key_id, field='start_key_id')
    target_id = _positive_int(target_key_id, field='target_key_id')
    if start_id == target_id:
        return True
    with get_db() as conn:
        return _binding_path_exists(
            conn,
            start_key_id=start_id,
            target_key_id=target_id,
        )


def get_subscription_host_candidates(
    *,
    user_id: int,
    group_id: int,
) -> list[dict[str, Any]]:
    """Returns configured candidate keys; domain policy filters capability/activity."""
    owner_id = _positive_int(user_id, field='user_id')
    tariff_group_id = _positive_int(group_id, field='group_id')
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                vk.id, vk.user_id, vk.tariff_id, vk.custom_name,
                vk.expires_at, vk.traffic_used, vk.traffic_limit,
                vk.panel_email, vk.sub_id, vk.server_id,
                t.name AS tariff_name, t.group_id AS tariff_group_id,
                s.name AS server_name, s.is_active AS server_active,
                s.panel_version
            FROM vpn_keys vk
            JOIN tariffs t ON t.id = vk.tariff_id
            JOIN servers s ON s.id = vk.server_id
            WHERE vk.user_id = ?
              AND t.group_id = ?
              AND vk.panel_email IS NOT NULL
              AND vk.panel_email != ''
              AND vk.sub_id IS NOT NULL
              AND vk.sub_id != ''
            ORDER BY vk.id
            """,
            (owner_id, tariff_group_id),
        ).fetchall()
        return [dict(row) for row in rows]


def enqueue_subscription_composition_sync(
    *,
    host_key_id: int,
) -> Optional[dict[str, Any]]:
    """Marks a host dirty and advances its monotonic desired revision."""
    host_id = _positive_int(host_key_id, field='host_key_id')
    with get_db() as conn:
        host_exists = conn.execute(
            'SELECT 1 FROM vpn_keys WHERE id = ?',
            (host_id,),
        ).fetchone()
        if host_exists is None:
            return None
        conn.execute(
            """
            INSERT INTO subscription_composition_sync (
                host_key_id, state, desired_revision, applied_revision,
                applied_tokens_json, attempts, next_attempt_at, lease_until,
                last_attempt_at, last_error_code, updated_at
            )
            VALUES (?, 'pending', 1, 0, '[]', 0, NULL, NULL, NULL, NULL,
                    CURRENT_TIMESTAMP)
            ON CONFLICT(host_key_id) DO UPDATE SET
                state = CASE
                    WHEN subscription_composition_sync.lease_until > CURRENT_TIMESTAMP
                    THEN subscription_composition_sync.state
                    ELSE 'pending'
                END,
                desired_revision = subscription_composition_sync.desired_revision + 1,
                attempts = CASE
                    WHEN subscription_composition_sync.lease_until > CURRENT_TIMESTAMP
                    THEN subscription_composition_sync.attempts
                    ELSE 0
                END,
                next_attempt_at = NULL,
                lease_until = CASE
                    WHEN subscription_composition_sync.lease_until > CURRENT_TIMESTAMP
                    THEN subscription_composition_sync.lease_until
                    ELSE NULL
                END,
                lease_owner_token = CASE
                    WHEN subscription_composition_sync.lease_until > CURRENT_TIMESTAMP
                    THEN subscription_composition_sync.lease_owner_token
                    ELSE NULL
                END,
                last_error_code = NULL,
                updated_at = CURRENT_TIMESTAMP
            """,
            (host_id,),
        )
        return _get_sync_state(conn, host_id)


def enqueue_subscription_composition_drift() -> int:
    """Wakes settled hosts without moving already queued work to the tail.

    A global drift pass must not continuously refresh ``updated_at`` for
    pending/retrying rows: doing so lets the same low-id batch starve the rest
    of a large queue. Synced and blocked rows are settled and may safely start
    a new desired revision.
    """
    with get_db() as conn:
        cursor = conn.execute(
            """
            UPDATE subscription_composition_sync
            SET state = 'pending',
                desired_revision = desired_revision + 1,
                attempts = 0,
                next_attempt_at = NULL,
                lease_until = NULL,
                lease_owner_token = NULL,
                last_error_code = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE state IN ('synced', 'blocked')
              AND (lease_until IS NULL OR lease_until <= CURRENT_TIMESTAMP)
            """
        )
        return max(0, int(cursor.rowcount or 0))


def get_subscription_composition_sync_state(
    *,
    host_key_id: int,
) -> Optional[dict[str, Any]]:
    """Returns durable reconciliation state for one host."""
    host_id = _positive_int(host_key_id, field='host_key_id')
    with get_db() as conn:
        return _get_sync_state(conn, host_id)


def list_subscription_composition_sync_states() -> list[dict[str, Any]]:
    """Lists every durable host state for periodic drift/capability recovery."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM subscription_composition_sync
            ORDER BY host_key_id
            """
        ).fetchall()
        return [_sync_result(row) for row in rows]


def list_due_subscription_composition_hosts(
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Lists dirty, due and currently unleased hosts for a background worker."""
    normalized_limit = _bounded_positive_int(limit, field='limit', maximum=1000)
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM subscription_composition_sync
            WHERE state IN ('pending', 'retrying')
              AND desired_revision > applied_revision
              AND (next_attempt_at IS NULL OR next_attempt_at <= CURRENT_TIMESTAMP)
              AND (lease_until IS NULL OR lease_until <= CURRENT_TIMESTAMP)
            ORDER BY updated_at, host_key_id
            LIMIT ?
            """,
            (normalized_limit,),
        ).fetchall()
        return [_sync_result(row) for row in rows]


def claim_subscription_composition_sync(
    *,
    host_key_id: int,
    lease_seconds: int = 60,
    lease_owner_token: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Atomically leases one due host and returns the claimed revision snapshot."""
    host_id = _positive_int(host_key_id, field='host_key_id')
    lease = _bounded_positive_int(
        lease_seconds,
        field='lease_seconds',
        maximum=3600,
    )
    owner_token = (
        secrets.token_urlsafe(24)
        if lease_owner_token is None
        else _normalize_lease_owner_token(lease_owner_token)
    )
    modifier = f'+{lease} seconds'
    with get_db() as conn:
        cursor = conn.execute(
            """
            UPDATE subscription_composition_sync
            SET state = 'retrying',
                attempts = attempts + 1,
                last_attempt_at = CURRENT_TIMESTAMP,
                lease_until = datetime('now', ?),
                lease_owner_token = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE host_key_id = ?
              AND state IN ('pending', 'retrying')
              AND desired_revision > applied_revision
              AND (next_attempt_at IS NULL OR next_attempt_at <= CURRENT_TIMESTAMP)
              AND (lease_until IS NULL OR lease_until <= CURRENT_TIMESTAMP)
            """,
            (modifier, owner_token, host_id),
        )
        if cursor.rowcount <= 0:
            return None
        return _get_sync_state(conn, host_id)


def renew_subscription_composition_sync_lease(
    *,
    host_key_id: int,
    lease_owner_token: str,
    desired_revision: int,
    lease_seconds: int = 120,
) -> bool:
    """Renews the exact live claim before a full-replacement panel write.

    The desired revision is part of the fence: a claimant that built an older
    graph cannot extend its right to write after the graph advanced.
    """
    host_id = _positive_int(host_key_id, field='host_key_id')
    owner_token = _normalize_lease_owner_token(lease_owner_token)
    revision = _non_negative_int(desired_revision, field='desired_revision')
    lease = _bounded_positive_int(
        lease_seconds,
        field='lease_seconds',
        maximum=3600,
    )
    modifier = f'+{lease} seconds'
    with get_db() as conn:
        cursor = conn.execute(
            """
            UPDATE subscription_composition_sync
            SET lease_until = datetime('now', ?),
                updated_at = CURRENT_TIMESTAMP
            WHERE host_key_id = ?
              AND lease_owner_token = ?
              AND lease_until > CURRENT_TIMESTAMP
              AND desired_revision = ?
              AND desired_revision > applied_revision
            """,
            (modifier, host_id, owner_token, revision),
        )
        return cursor.rowcount > 0


def release_subscription_composition_sync_lease(
    *,
    host_key_id: int,
    lease_owner_token: str,
) -> bool:
    """Releases only the caller's claim and leaves newer work pending."""
    host_id = _positive_int(host_key_id, field='host_key_id')
    owner_token = _normalize_lease_owner_token(lease_owner_token)
    with get_db() as conn:
        cursor = conn.execute(
            """
            UPDATE subscription_composition_sync
            SET state = CASE
                    WHEN desired_revision > applied_revision THEN 'pending'
                    ELSE 'synced'
                END,
                next_attempt_at = NULL,
                lease_until = NULL,
                lease_owner_token = NULL,
                last_error_code = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE host_key_id = ?
              AND lease_owner_token = ?
            """,
            (host_id, owner_token),
        )
        return cursor.rowcount > 0


def mark_subscription_composition_sync_result(
    *,
    host_key_id: int,
    desired_revision: int,
    applied_tokens: Iterable[str],
    lease_owner_token: str,
    host_fingerprint: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Records panel success without erasing a newer desired revision."""
    host_id = _positive_int(host_key_id, field='host_key_id')
    revision = _non_negative_int(desired_revision, field='desired_revision')
    tokens_json = _encode_applied_tokens(applied_tokens)
    fingerprint = _normalize_host_fingerprint(host_fingerprint)
    owner_token = _normalize_lease_owner_token(lease_owner_token)
    with get_db() as conn:
        cursor = conn.execute(
            """
            UPDATE subscription_composition_sync
            SET applied_revision = MAX(applied_revision, ?),
                applied_tokens_json = ?,
                applied_host_fingerprint = ?,
                state = CASE
                    WHEN desired_revision <= ? THEN 'synced'
                    ELSE 'pending'
                END,
                attempts = CASE
                    WHEN desired_revision <= ? THEN 0
                    ELSE attempts
                END,
                next_attempt_at = NULL,
                lease_until = NULL,
                lease_owner_token = NULL,
                last_error_code = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE host_key_id = ?
              AND applied_revision <= ?
              AND lease_owner_token = ?
            """,
            (
                revision,
                tokens_json,
                fingerprint,
                revision,
                revision,
                host_id,
                revision,
                owner_token,
            ),
        )
        if cursor.rowcount <= 0:
            return _get_sync_state(conn, host_id)
        return _get_sync_state(conn, host_id)


def mark_subscription_composition_sync_partial(
    *,
    host_key_id: int,
    desired_revision: int,
    applied_tokens: Iterable[str],
    error_code: str,
    retry_after_seconds: Optional[int],
    host_fingerprint: Optional[str] = None,
    lease_owner_token: str,
) -> Optional[dict[str, Any]]:
    """Records the exact partial panel marker set without advancing revision."""
    host_id = _positive_int(host_key_id, field='host_key_id')
    revision = _non_negative_int(desired_revision, field='desired_revision')
    tokens_json = _encode_applied_tokens(applied_tokens)
    normalized_error = _normalize_error_code(error_code)
    fingerprint = _normalize_host_fingerprint(host_fingerprint)
    owner_token = _normalize_lease_owner_token(lease_owner_token)
    if retry_after_seconds is None:
        retry_modifier = None
    else:
        retry_after = _bounded_positive_int(
            retry_after_seconds,
            field='retry_after_seconds',
            maximum=86400,
        )
        retry_modifier = f'+{retry_after} seconds'
    with get_db() as conn:
        cursor = conn.execute(
            """
            UPDATE subscription_composition_sync
            SET applied_tokens_json = ?,
                applied_host_fingerprint = ?,
                state = CASE
                    WHEN desired_revision > ? THEN 'pending'
                    WHEN ? IS NULL THEN 'blocked'
                    ELSE 'retrying'
                END,
                next_attempt_at = CASE
                    WHEN desired_revision > ? OR ? IS NULL THEN NULL
                    ELSE datetime('now', ?)
                END,
                lease_until = NULL,
                lease_owner_token = NULL,
                last_error_code = CASE
                    WHEN desired_revision > ? THEN NULL
                    ELSE ?
                END,
                updated_at = CURRENT_TIMESTAMP
            WHERE host_key_id = ?
              AND lease_owner_token = ?
              AND applied_revision <= ?
            """,
            (
                tokens_json,
                fingerprint,
                revision,
                retry_modifier,
                revision,
                retry_modifier,
                retry_modifier,
                revision,
                normalized_error,
                host_id,
                owner_token,
                revision,
            ),
        )
        if cursor.rowcount <= 0:
            return _get_sync_state(conn, host_id)
        return _get_sync_state(conn, host_id)


def mark_subscription_composition_sync_retry(
    *,
    host_key_id: int,
    error_code: str,
    retry_after_seconds: int,
    lease_owner_token: str,
    desired_revision: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Releases a lease and schedules a bounded retry after a transient error."""
    host_id = _positive_int(host_key_id, field='host_key_id')
    retry_after = _bounded_positive_int(
        retry_after_seconds,
        field='retry_after_seconds',
        maximum=86400,
    )
    normalized_error = _normalize_error_code(error_code)
    expected_revision = (
        None
        if desired_revision is None
        else _non_negative_int(desired_revision, field='desired_revision')
    )
    modifier = f'+{retry_after} seconds'
    owner_token = _normalize_lease_owner_token(lease_owner_token)
    with get_db() as conn:
        if expected_revision is None:
            cursor = conn.execute(
                """
                UPDATE subscription_composition_sync
                SET state = 'retrying',
                    next_attempt_at = datetime('now', ?),
                    lease_until = NULL,
                    lease_owner_token = NULL,
                    last_error_code = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE host_key_id = ?
                  AND lease_owner_token = ?
                """,
                (modifier, normalized_error, host_id, owner_token),
            )
        else:
            cursor = conn.execute(
                """
                UPDATE subscription_composition_sync
                SET state = CASE
                        WHEN desired_revision > ? THEN 'pending'
                        ELSE 'retrying'
                    END,
                    next_attempt_at = CASE
                        WHEN desired_revision > ? THEN NULL
                        ELSE datetime('now', ?)
                    END,
                    lease_until = NULL,
                    lease_owner_token = NULL,
                    last_error_code = CASE
                        WHEN desired_revision > ? THEN NULL
                        ELSE ?
                    END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE host_key_id = ?
                  AND lease_owner_token = ?
                """,
                (
                    expected_revision,
                    expected_revision,
                    modifier,
                    expected_revision,
                    normalized_error,
                    host_id,
                    owner_token,
                ),
            )
        if cursor.rowcount <= 0:
            return None
        return _get_sync_state(conn, host_id)


def mark_subscription_composition_sync_blocked(
    *,
    host_key_id: int,
    error_code: str,
    lease_owner_token: str,
    desired_revision: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Marks reconciliation blocked until another lifecycle enqueue occurs."""
    host_id = _positive_int(host_key_id, field='host_key_id')
    normalized_error = _normalize_error_code(error_code)
    expected_revision = (
        None
        if desired_revision is None
        else _non_negative_int(desired_revision, field='desired_revision')
    )
    owner_token = _normalize_lease_owner_token(lease_owner_token)
    with get_db() as conn:
        if expected_revision is None:
            cursor = conn.execute(
                """
                UPDATE subscription_composition_sync
                SET state = 'blocked',
                    next_attempt_at = NULL,
                    lease_until = NULL,
                    lease_owner_token = NULL,
                    last_error_code = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE host_key_id = ?
                  AND lease_owner_token = ?
                """,
                (normalized_error, host_id, owner_token),
            )
        else:
            cursor = conn.execute(
                """
                UPDATE subscription_composition_sync
                SET state = CASE
                        WHEN desired_revision > ? THEN 'pending'
                        ELSE 'blocked'
                    END,
                    next_attempt_at = NULL,
                    lease_until = NULL,
                    lease_owner_token = NULL,
                    last_error_code = CASE
                        WHEN desired_revision > ? THEN NULL
                        ELSE ?
                    END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE host_key_id = ?
                  AND lease_owner_token = ?
                """,
                (
                    expected_revision,
                    expected_revision,
                    normalized_error,
                    host_id,
                    owner_token,
                ),
            )
        if cursor.rowcount <= 0:
            return None
        return _get_sync_state(conn, host_id)


def _get_binding_by_pair(
    conn: sqlite3.Connection,
    host_key_id: int,
    component_key_id: int,
) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT *
        FROM subscription_bindings
        WHERE host_key_id = ? AND component_key_id = ?
        """,
        (host_key_id, component_key_id),
    ).fetchone()


def _binding_path_exists(
    conn: sqlite3.Connection,
    *,
    start_key_id: int,
    target_key_id: int,
) -> bool:
    row = conn.execute(
        """
        WITH RECURSIVE reachable(key_id) AS (
            SELECT component_key_id
            FROM subscription_bindings
            WHERE host_key_id = ?
            UNION
            SELECT child.component_key_id
            FROM subscription_bindings child
            JOIN reachable parent ON child.host_key_id = parent.key_id
        )
        SELECT 1
        FROM reachable
        WHERE key_id = ?
        LIMIT 1
        """,
        (start_key_id, target_key_id),
    ).fetchone()
    return row is not None


def _get_sync_state(
    conn: sqlite3.Connection,
    host_key_id: int,
) -> Optional[dict[str, Any]]:
    row = conn.execute(
        """
        SELECT *
        FROM subscription_composition_sync
        WHERE host_key_id = ?
        """,
        (host_key_id,),
    ).fetchone()
    return _sync_result(row) if row is not None else None


def _binding_result(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _sync_result(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    raw_tokens = result.get('applied_tokens_json')
    tokens: list[str] = []
    if raw_tokens:
        try:
            decoded = json.loads(str(raw_tokens))
        except (TypeError, json.JSONDecodeError):
            decoded = []
        if isinstance(decoded, list):
            tokens = [str(item) for item in decoded if isinstance(item, str)]
    result['applied_tokens'] = tokens
    return result


def _encode_applied_tokens(values: Iterable[str]) -> str:
    if isinstance(values, (str, bytes)):
        raise ValueError('applied_tokens must be an iterable of token strings')
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError('applied_tokens must contain only strings')
        token = value.strip()
        if not token or len(token) > 256:
            raise ValueError('applied token must contain 1 to 256 characters')
        normalized.add(token)
    return json.dumps(sorted(normalized), ensure_ascii=True, separators=(',', ':'))


def _normalize_error_code(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError('error_code must be a string')
    code = value.strip().casefold()
    if not re.fullmatch(r'[a-z][a-z0-9_.:-]{0,127}', code):
        raise ValueError('error_code has an invalid format')
    return code


def _normalize_source_reference(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('source_reference must be a string')
    reference = value.strip()
    if not reference or len(reference) > 256 or any(ord(char) < 32 for char in reference):
        raise ValueError('source_reference must contain 1 to 256 printable characters')
    return reference


def _normalize_host_fingerprint(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('host_fingerprint must be a string')
    fingerprint = value.strip()
    if not fingerprint or len(fingerprint) > 256:
        raise ValueError('host_fingerprint must contain 1 to 256 characters')
    return fingerprint


def _normalize_lease_owner_token(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError('lease_owner_token must be a string')
    token = value.strip()
    if (
        not token
        or len(token) > 128
        or not re.fullmatch(r'[A-Za-z0-9_-]+', token)
    ):
        raise ValueError('lease_owner_token has an invalid format')
    return token


def _positive_int(value: Any, *, field: str) -> int:
    normalized = _non_negative_int(value, field=field)
    if normalized <= 0:
        raise ValueError(f'{field} must be a positive integer')
    return normalized


def _non_negative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f'{field} must be a non-negative integer')
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{field} must be a non-negative integer') from exc
    if normalized < 0:
        raise ValueError(f'{field} must be a non-negative integer')
    return normalized


def _bounded_positive_int(value: Any, *, field: str, maximum: int) -> int:
    normalized = _positive_int(value, field=field)
    if normalized > maximum:
        raise ValueError(f'{field} must not exceed {maximum}')
    return normalized
