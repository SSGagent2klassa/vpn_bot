"""Domain policy for composing component subscriptions into host keys."""

from __future__ import annotations

import asyncio
import importlib
import logging
from typing import Any, Optional

from bot.utils.panel_version import (
    CLIENT_EXTERNAL_LINKS_MIN_VERSION,
    panel_version_at_least,
)
from database import requests as db

logger = logging.getLogger(__name__)

CORE_GROUP_PARENT_SOURCE = 'core.group_parent'
_RECONCILER_MODULE = 'bot.services.subscription_composition_reconcile'
_PUBLIC_SYNC_STATES = frozenset({'pending', 'synced', 'retrying', 'blocked'})
_RECONCILE_TASKS: set[asyncio.Task[Any]] = set()

__all__ = [
    'CORE_GROUP_PARENT_SOURCE',
    'bind_key_subscription',
    'get_default_subscription_hosts',
    'list_key_subscription_bindings',
    'list_key_subscription_reconcile_host_ids',
    'list_subscription_key_summaries',
    'request_key_subscription_reconcile',
    'schedule_key_subscription_reconciles',
    'schedule_subscription_host_reconciles',
    'unbind_key_subscription',
]


def get_default_subscription_hosts(
    *,
    component_key_id: int,
) -> list[dict[str, Any]]:
    """Returns every suitable host in the component group's configured parent.

    This resolver deliberately returns 0/1/N candidates. Selection policy is a
    caller concern; the core neither limits key counts nor guesses one host when
    several valid hosts exist.
    """
    component_id = _normalize_key_id(component_key_id)
    if component_id is None:
        return []
    component = db.get_vpn_key_by_id(component_id)
    if component is None:
        return []
    if db.list_component_subscription_bindings(component_key_id=component_id):
        return []

    component_group_id = _optional_positive_int(component.get('tariff_group_id'))
    if component_group_id is None:
        return []
    component_group = db.get_group_by_id(component_group_id)
    if component_group is None:
        return []
    parent_group_id = _optional_positive_int(
        component_group.get('subscription_parent_group_id')
    )
    if parent_group_id is None:
        return []

    owner_id = _optional_positive_int(component.get('user_id'))
    if owner_id is None:
        return []

    hosts: list[dict[str, Any]] = []
    for candidate in db.get_subscription_host_candidates(
        user_id=owner_id,
        group_id=parent_group_id,
    ):
        candidate_id = _optional_positive_int(candidate.get('id'))
        if candidate_id is None or candidate_id == component_id:
            continue
        if _strict_host_rejection(candidate) is not None:
            continue
        if db.subscription_binding_path_exists(
            start_key_id=component_id,
            target_key_id=candidate_id,
        ):
            continue
        hosts.append(candidate)
    return hosts


async def bind_key_subscription(
    *,
    host_key_id: int,
    component_key_id: int,
    source_namespace: str,
    owner_user_id: Optional[int] = None,
    source_reference: Optional[str] = None,
) -> dict[str, Any]:
    """Persists one desired relation and schedules best-effort reconciliation.

    Generic extension callers may persist a relation while either key is still
    a draft. The stock ``core.group_parent`` flow additionally enforces the
    configured tariff-group parent and a currently suitable host.
    """
    host_id = _normalize_key_id(host_key_id)
    component_id = _normalize_key_id(component_key_id)
    if host_id is None or component_id is None:
        return _rejected('invalid_key_id')
    try:
        namespace = db.normalize_subscription_source_namespace(source_namespace)
    except ValueError:
        return _rejected('invalid_source_namespace')
    try:
        reference = db.normalize_subscription_source_reference(source_reference)
    except ValueError:
        return _rejected('invalid_source_reference')
    owner_id = (
        None if owner_user_id is None else _normalize_key_id(owner_user_id)
    )
    if owner_user_id is not None and owner_id is None:
        return _rejected('invalid_owner_user_id')
    if host_id == component_id:
        return _rejected('self_binding')

    host = db.get_vpn_key_by_id(host_id)
    if host is None:
        return _rejected('host_key_not_found')
    component = db.get_vpn_key_by_id(component_id)
    if component is None:
        return _rejected('component_key_not_found')
    if _optional_positive_int(host.get('user_id')) != _optional_positive_int(
        component.get('user_id')
    ):
        return _rejected('owner_mismatch')
    if owner_id is not None and (
        _optional_positive_int(host.get('user_id')) != owner_id
        or _optional_positive_int(component.get('user_id')) != owner_id
    ):
        return _rejected('owner_mismatch')

    existing = db.get_subscription_binding_by_pair(
        host_key_id=host_id,
        component_key_id=component_id,
    )
    if existing is not None:
        state = _ensure_sync_state(host_id)
        return _success_result(
            binding_id=int(existing['id']),
            status=state,
            applied=False,
            already_applied=True,
        )

    if db.subscription_binding_path_exists(
        start_key_id=component_id,
        target_key_id=host_id,
    ):
        return _rejected('cycle_detected')

    if namespace == CORE_GROUP_PARENT_SOURCE:
        group_error = _group_parent_rejection(host=host, component=component)
        if group_error is not None:
            return _rejected(group_error)
        host_error = _strict_host_rejection(host)
        if host_error is not None:
            return _rejected(host_error)

    try:
        binding = db.create_subscription_binding(
            host_key_id=host_id,
            component_key_id=component_id,
            source_namespace=namespace,
            source_reference=reference,
            exclusive_component=(namespace == CORE_GROUP_PARENT_SOURCE),
        )
    except ValueError as exc:
        return _rejected(_repository_error_code(exc))

    created = bool(binding.get('created'))
    state = _ensure_sync_state(host_id)
    if created:
        _schedule_reconcile(host_id)
    return _success_result(
        binding_id=int(binding['id']),
        status=state,
        applied=created,
        already_applied=not created,
    )


async def unbind_key_subscription(
    *,
    host_key_id: int,
    component_key_id: int,
    source_namespace: Optional[str] = None,
    owner_user_id: Optional[int] = None,
) -> dict[str, Any]:
    """Removes one exact desired relation and schedules panel cleanup.

    ``source_namespace`` is an optional internal audit guard. Public facade
    callers omit it and may remove any exact relation between their own keys.
    """
    host_id = _normalize_key_id(host_key_id)
    component_id = _normalize_key_id(component_key_id)
    if host_id is None or component_id is None:
        return _rejected('invalid_key_id')
    if source_namespace is None:
        namespace = None
    else:
        try:
            namespace = db.normalize_subscription_source_namespace(source_namespace)
        except ValueError:
            return _rejected('invalid_source_namespace')
    owner_id = (
        None if owner_user_id is None else _normalize_key_id(owner_user_id)
    )
    if owner_user_id is not None and owner_id is None:
        return _rejected('invalid_owner_user_id')

    host = db.get_vpn_key_by_id(host_id)
    if host is None:
        return _rejected('host_key_not_found')
    if owner_id is not None and _optional_positive_int(host.get('user_id')) != owner_id:
        return _rejected('owner_mismatch')

    existing = db.get_subscription_binding_by_pair(
        host_key_id=host_id,
        component_key_id=component_id,
    )
    if existing is None:
        sync = db.get_subscription_composition_sync_state(host_key_id=host_id)
        status = str((sync or {}).get('state') or 'synced')
        if status not in _PUBLIC_SYNC_STATES:
            status = 'pending'
        return _success_result(
            binding_id=None,
            status=status,
            applied=False,
            already_applied=True,
        )
    if (
        namespace is not None
        and str(existing.get('source_namespace') or '') != namespace
    ):
        return _rejected('source_namespace_mismatch')

    component = db.get_vpn_key_by_id(component_id)
    if component is None:
        return _rejected('component_key_not_found')
    if _optional_positive_int(host.get('user_id')) != _optional_positive_int(
        component.get('user_id')
    ):
        return _rejected('owner_mismatch')
    if owner_id is not None and _optional_positive_int(component.get('user_id')) != owner_id:
        return _rejected('owner_mismatch')

    removed = db.unbind_subscription_binding(
        host_key_id=host_id,
        component_key_id=component_id,
        source_namespace=namespace,
    )
    if removed is None:
        return _success_result(
            binding_id=None,
            status=_ensure_sync_state(host_id),
            applied=False,
            already_applied=True,
        )
    state = _ensure_sync_state(host_id)
    _schedule_reconcile(host_id)
    return _success_result(
        binding_id=int(removed['id']),
        status=state,
        applied=True,
        already_applied=False,
    )


async def request_key_subscription_reconcile(
    *,
    host_key_id: int,
    owner_user_id: int,
) -> dict[str, Any]:
    """Advances desired revision for an owner-verified host and schedules sync."""
    host_id = _normalize_key_id(host_key_id)
    owner_id = _normalize_key_id(owner_user_id)
    if host_id is None or owner_id is None:
        return _rejected('invalid_key_id')
    host = db.get_vpn_key_by_id(host_id)
    if host is None:
        return _rejected('host_key_not_found')
    if _optional_positive_int(host.get('user_id')) != owner_id:
        return _rejected('owner_mismatch')
    sync = db.enqueue_subscription_composition_sync(host_key_id=host_id)
    if sync is None:
        return _rejected('host_key_not_found')
    _schedule_reconcile(host_id)
    return {
        'ok': True,
        'status': 'pending',
        'host_key_id': host_id,
        'binding_id': None,
        'applied': True,
        'already_applied': False,
        'error_code': None,
    }


def list_key_subscription_reconcile_host_ids(
    *,
    key_id: int,
    include_self: bool = True,
) -> tuple[int, ...]:
    """Returns hosts affected when one key's transport identity changes."""
    normalized_key_id = _required_positive_int(key_id, field='key_id')
    host_ids = {
        int(row['host_key_id'])
        for row in db.list_component_subscription_bindings(
            component_key_id=normalized_key_id,
        )
    }
    if include_self and db.get_subscription_composition_sync_state(
        host_key_id=normalized_key_id,
    ) is not None:
        host_ids.add(normalized_key_id)
    return tuple(sorted(host_ids))


def schedule_subscription_host_reconciles(
    *,
    host_key_ids: Any,
) -> int:
    """Best-effort starts for already durable host synchronization rows."""
    if isinstance(host_key_ids, (str, bytes)):
        raise ValueError('host_key_ids must be an iterable of key ids')
    normalized_ids = {
        _required_positive_int(value, field='host_key_id')
        for value in host_key_ids
    }
    scheduled = 0
    for host_id in sorted(normalized_ids):
        if db.get_subscription_composition_sync_state(host_key_id=host_id) is None:
            continue
        _schedule_reconcile(host_id)
        scheduled += 1
    return scheduled


def schedule_key_subscription_reconciles(*, key_id: int) -> int:
    """Best-effort wakes a key as host and every host that contains it."""
    return schedule_subscription_host_reconciles(
        host_key_ids=list_key_subscription_reconcile_host_ids(key_id=key_id),
    )


def list_key_subscription_bindings(
    *,
    owner_user_id: int,
    key_id: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Returns owner-scoped bindings without panel identities or managed tokens."""
    owner_id = _required_positive_int(owner_user_id, field='owner_user_id')
    normalized_key_id = (
        None if key_id is None else _required_positive_int(key_id, field='key_id')
    )
    rows = db.list_owner_subscription_bindings(
        owner_user_id=owner_id,
        key_id=normalized_key_id,
    )
    return [
        {
            'id': int(row['id']),
            'binding_id': int(row['id']),
            'host_key_id': int(row['host_key_id']),
            'component_key_id': int(row['component_key_id']),
            'source_namespace': str(row['source_namespace']),
            'status': str(row.get('sync_state') or 'pending'),
            'created_at': row.get('created_at'),
            'updated_at': row.get('updated_at'),
        }
        for row in rows
    ]


def list_subscription_key_summaries(
    *,
    owner_user_id: int,
) -> list[dict[str, Any]]:
    """Returns safe key metadata for an extension-owned selection workflow."""
    owner_id = _required_positive_int(owner_user_id, field='owner_user_id')
    summaries: list[dict[str, Any]] = []
    for row in db.list_subscription_keys_for_owner(owner_user_id=owner_id):
        configured = bool(row.get('is_configured'))
        server_active = bool(row.get('server_active'))
        summaries.append(
            {
                'id': int(row['id']),
                'key_id': int(row['id']),
                'custom_name': row.get('custom_name'),
                'tariff_id': row.get('tariff_id'),
                'tariff_name': row.get('tariff_name'),
                'tariff_group_id': row.get('tariff_group_id'),
                'expires_at': row.get('expires_at'),
                'traffic_used': int(row.get('traffic_used') or 0),
                'traffic_limit': int(row.get('traffic_limit') or 0),
                'server_id': row.get('server_id'),
                'server_name': row.get('server_name'),
                'is_configured': configured,
                'is_active': bool(db.is_key_active(row)),
                'server_active': server_active,
                'subscription_host_capable': bool(
                    configured
                    and server_active
                    and panel_version_at_least(
                        row.get('panel_version'),
                        CLIENT_EXTERNAL_LINKS_MIN_VERSION,
                    )
                ),
            }
        )
    return summaries


def _group_parent_rejection(
    *,
    host: dict[str, Any],
    component: dict[str, Any],
) -> Optional[str]:
    component_group_id = _optional_positive_int(component.get('tariff_group_id'))
    host_group_id = _optional_positive_int(host.get('tariff_group_id'))
    if component_group_id is None or host_group_id is None:
        return 'tariff_group_missing'
    component_group = db.get_group_by_id(component_group_id)
    if component_group is None:
        return 'tariff_group_missing'
    parent_group_id = _optional_positive_int(
        component_group.get('subscription_parent_group_id')
    )
    if parent_group_id is None:
        return 'subscription_parent_missing'
    if parent_group_id != host_group_id:
        return 'group_parent_mismatch'
    return None


def _strict_host_rejection(host: dict[str, Any]) -> Optional[str]:
    if (
        _optional_positive_int(host.get('server_id')) is None
        or not str(host.get('panel_email') or '').strip()
        or not str(host.get('sub_id') or '').strip()
    ):
        return 'host_not_configured'
    if not bool(host.get('server_active')):
        return 'host_server_inactive'
    if not db.is_key_active(host):
        return 'host_inactive'
    if not panel_version_at_least(
        host.get('panel_version'),
        CLIENT_EXTERNAL_LINKS_MIN_VERSION,
    ):
        return 'host_panel_unsupported'
    return None


def _ensure_sync_state(host_key_id: int) -> str:
    sync = db.get_subscription_composition_sync_state(host_key_id=host_key_id)
    if sync is None:
        sync = db.enqueue_subscription_composition_sync(host_key_id=host_key_id)
    state = str((sync or {}).get('state') or 'pending')
    return state if state in _PUBLIC_SYNC_STATES else 'pending'


def _schedule_reconcile(host_key_id: int) -> None:
    """Starts an optional reconciler without weakening the durable DB write."""
    try:
        module = importlib.import_module(_RECONCILER_MODULE)
        reconcile = getattr(module, 'reconcile_subscription_host')
    except ModuleNotFoundError as exc:
        if exc.name != _RECONCILER_MODULE:
            logger.exception('Subscription composition reconciler import failed')
        return
    except (AttributeError, ImportError):
        logger.exception('Subscription composition reconciler import failed')
        return
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(reconcile(host_key_id=host_key_id))
    except (RuntimeError, TypeError):
        logger.exception(
            'Could not schedule subscription composition for host key %s',
            host_key_id,
        )
        return
    _RECONCILE_TASKS.add(task)
    task.add_done_callback(
        lambda completed: _consume_reconcile_result(completed, host_key_id)
    )


def _consume_reconcile_result(task: asyncio.Task[Any], host_key_id: int) -> None:
    _RECONCILE_TASKS.discard(task)
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception(
            'Subscription composition reconcile failed for host key %s',
            host_key_id,
        )


def _success_result(
    *,
    binding_id: Optional[int],
    status: str,
    applied: bool,
    already_applied: bool,
) -> dict[str, Any]:
    return {
        'ok': True,
        'status': status,
        'binding_id': binding_id,
        'applied': applied,
        'already_applied': already_applied,
        'error_code': None,
    }


def _rejected(error_code: str) -> dict[str, Any]:
    return {
        'ok': False,
        'status': 'rejected',
        'binding_id': None,
        'applied': False,
        'already_applied': False,
        'error_code': error_code,
    }


def _repository_error_code(exc: ValueError) -> str:
    message = str(exc).casefold()
    if 'same owner' in message:
        return 'owner_mismatch'
    if 'cycle' in message:
        return 'cycle_detected'
    if 'own subscription' in message:
        return 'self_binding'
    if 'host_key_id does not exist' in message:
        return 'host_key_not_found'
    if 'component_key_id does not exist' in message:
        return 'component_key_not_found'
    if 'already has a subscription binding' in message:
        return 'component_already_bound'
    return 'invalid_binding'


def _normalize_key_id(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def _optional_positive_int(value: Any) -> Optional[int]:
    return _normalize_key_id(value)


def _required_positive_int(value: Any, *, field: str) -> int:
    normalized = _normalize_key_id(value)
    if normalized is None:
        raise ValueError(f'{field} must be a positive integer')
    return normalized
