"""Durable PaymentIntent-origin completion delivery for custom extensions."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from database.requests import (
    claim_extension_completion_job,
    complete_extension_completion_job,
    degrade_extension_completion_job,
    ensure_extension_completion_job,
    get_due_extension_completion_job_ids,
    mark_extension_completion_ready,
    promote_configured_extension_completion_jobs,
    retry_extension_completion_job,
)

logger = logging.getLogger(__name__)

COMPLETION_BATCH_LIMIT = 100
COMPLETION_CONCURRENCY = 8
COMPLETION_HANDLER_TIMEOUT_SECONDS = 12
_RETRY_OFFSETS_SECONDS = (60, 300, 900, 3600, 21600, 86400)


def ensure_payment_origin_completion_job(order_id: str, *, key_id: int) -> bool:
    """Persist the completion job before a key-purchase fulfillment is finalized."""
    return ensure_extension_completion_job(str(order_id), key_id=int(key_id)) is not None


def promote_payment_origin_completion_after_fulfillment(
    order_id: str,
    *,
    key_id: int,
) -> bool:
    """Wake a configured origin job after its payment becomes financially final."""
    job = mark_extension_completion_ready(str(order_id), key_id=int(key_id))
    return bool(job is not None and str(job.get('state') or '') == 'ready')


async def run_extension_completion_after_key_configured(
    order_id: str,
    *,
    key_id: int,
    bot: Any = None,
) -> dict[str, Any]:
    """Promote and opportunistically deliver one configured key's durable job."""
    try:
        job = mark_extension_completion_ready(str(order_id), key_id=int(key_id))
        if job is None:
            return {'status': 'not_applicable', 'order_id': str(order_id)}
        result = await _deliver_job(int(job['id']), bot=bot)
        return {'order_id': str(order_id), **result}
    except Exception as exc:
        logger.error(
            'Configured-key extension completion failed order=%s key=%s type=%s',
            order_id,
            key_id,
            type(exc).__name__,
        )
        return {
            'status': 'retry_scheduled',
            'order_id': str(order_id),
            'error': type(exc).__name__,
        }


async def process_due_extension_completions(
    *,
    bot: Any = None,
    limit: int = COMPLETION_BATCH_LIMIT,
) -> dict[str, int]:
    """Recover configured jobs and process one bounded due batch."""
    batch = max(1, min(int(limit), COMPLETION_BATCH_LIMIT))
    promoted = promote_configured_extension_completion_jobs(limit=batch)
    job_ids = get_due_extension_completion_job_ids(limit=batch)
    semaphore = asyncio.Semaphore(COMPLETION_CONCURRENCY)

    async def deliver(job_id: int) -> dict[str, Any]:
        async with semaphore:
            return await _deliver_job(job_id, bot=bot)

    results = await asyncio.gather(
        *(deliver(job_id) for job_id in job_ids),
        return_exceptions=True,
    )
    summary = {
        'promoted': int(promoted),
        'queued': len(job_ids),
        'completed': 0,
        'retried': 0,
        'degraded': 0,
        'skipped': 0,
        'errors': 0,
    }
    for result in results:
        if isinstance(result, Exception):
            summary['errors'] += 1
            logger.error(
                'Unexpected extension completion worker failure type=%s',
                type(result).__name__,
            )
            continue
        status = str(result.get('status') or '')
        if status == 'completed':
            summary['completed'] += 1
        elif status == 'retry_scheduled':
            summary['retried'] += 1
        elif status == 'degraded':
            summary['degraded'] += 1
        else:
            summary['skipped'] += 1
    return summary


async def _deliver_job(job_id: int, *, bot: Any) -> dict[str, Any]:
    job = claim_extension_completion_job(int(job_id))
    if job is None:
        return {'status': 'skipped', 'job_id': int(job_id)}
    attempts = int(job.get('attempts') or 1)
    try:
        context = _build_handler_context(job)
    except ValueError:
        logger.error('Invalid durable completion snapshot job=%s', job_id)
        if not degrade_extension_completion_job(
            int(job_id),
            error_code='invalid_snapshot',
        ):
            raise RuntimeError('invalid completion snapshot could not be degraded')
        return {'status': 'degraded', 'job_id': int(job_id)}
    try:
        from bot.utils.extension_completion_registry import (
            dispatch_extension_completion,
        )

        acknowledgement = await asyncio.wait_for(
            dispatch_extension_completion(
                str(job['extension_id']),
                str(job['handler_name']),
                context,
                bot=bot,
            ),
            timeout=COMPLETION_HANDLER_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            'Extension completion handler timed out job=%s extension=%s handler=%s',
            job_id,
            job.get('extension_id'),
            job.get('handler_name'),
        )
        _schedule_retry(job_id, attempts, 'handler_timeout')
        return {'status': 'retry_scheduled', 'job_id': int(job_id)}
    except LookupError:
        logger.warning(
            'Extension completion handler unavailable job=%s extension=%s handler=%s',
            job_id,
            job.get('extension_id'),
            job.get('handler_name'),
        )
        _schedule_retry(job_id, attempts, 'handler_unavailable')
        return {'status': 'retry_scheduled', 'job_id': int(job_id)}
    except Exception as exc:
        logger.warning(
            'Extension completion handler failed job=%s extension=%s handler=%s type=%s',
            job_id,
            job.get('extension_id'),
            job.get('handler_name'),
            type(exc).__name__,
        )
        _schedule_retry(job_id, attempts, f'handler_error:{type(exc).__name__}')
        return {'status': 'retry_scheduled', 'job_id': int(job_id)}

    if acknowledgement.get('ok'):
        if not complete_extension_completion_job(int(job_id)):
            raise RuntimeError('completion acknowledgement could not be persisted')
        return {'status': 'completed', 'job_id': int(job_id)}
    reason = str(acknowledgement.get('reason') or 'handler_rejected')
    if acknowledgement.get('retry', True):
        _schedule_retry(job_id, attempts, 'handler_retry')
        return {
            'status': 'retry_scheduled',
            'job_id': int(job_id),
            'reason': reason,
        }
    if not degrade_extension_completion_job(
        int(job_id),
        error_code='handler_rejected',
    ):
        raise RuntimeError('completion rejection could not be persisted')
    return {'status': 'degraded', 'job_id': int(job_id), 'reason': reason}


def _build_handler_context(job: Mapping[str, Any]) -> dict[str, Any]:
    """Build the fixed public delivery contract from immutable persisted data."""
    version = int(job.get('origin_context_version') or 0)
    key_id = int(job.get('key_id') or 0)
    user_id = int(job.get('user_id') or 0)
    telegram_id = int(job.get('telegram_id') or 0)
    workflow_id = str(job.get('workflow_id') or '')
    order_id = str(job.get('order_id') or '')
    if version <= 0 or key_id <= 0 or user_id <= 0 or telegram_id <= 0:
        raise ValueError('completion identifiers must be positive')
    if not workflow_id or not order_id:
        raise ValueError('completion workflow/order id is missing')
    return {
        'event': 'key_configured',
        'version': version,
        'payload': dict(job.get('origin_payload') or {}),
        'workflow_id': workflow_id,
        'idempotency_key': workflow_id,
        'order_id': order_id,
        'key_id': key_id,
        'user_id': user_id,
        'telegram_id': telegram_id,
        'delivery_attempt': int(job.get('attempts') or 1),
    }


def _schedule_retry(job_id: int, attempts: int, error_code: str) -> None:
    offset = _RETRY_OFFSETS_SECONDS[
        min(max(1, int(attempts)) - 1, len(_RETRY_OFFSETS_SECONDS) - 1)
    ]
    if not retry_extension_completion_job(
        int(job_id),
        retry_after_seconds=offset,
        error_code=error_code,
    ):
        raise RuntimeError('completion retry could not be persisted')


__all__ = [
    'COMPLETION_BATCH_LIMIT',
    'COMPLETION_CONCURRENCY',
    'COMPLETION_HANDLER_TIMEOUT_SECONDS',
    'ensure_payment_origin_completion_job',
    'process_due_extension_completions',
    'promote_payment_origin_completion_after_fulfillment',
    'run_extension_completion_after_key_configured',
]
