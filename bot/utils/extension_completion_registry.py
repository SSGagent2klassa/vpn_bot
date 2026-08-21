"""Runtime registry for durable extension completion handlers."""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Mapping
from typing import Any, Awaitable

from bot.utils.action_origin_context import normalize_completion_handler_name
from database.db_extensions import normalize_extension_id

ExtensionCompletionHandler = Callable[
    [Mapping[str, Any]],
    Mapping[str, Any] | None | Awaitable[Mapping[str, Any] | None],
]

# ``extension_id.local_name`` -> callable. Persistence stores both parts separately.
EXTENSION_COMPLETION_HANDLERS: dict[str, ExtensionCompletionHandler] = {}


def register_extension_completion_handler(
    extension_id: str,
    handler_name: str,
    handler: ExtensionCompletionHandler,
    *,
    replace: bool = False,
) -> str:
    """Register one extension-owned durable completion handler."""
    ext_id = normalize_extension_id(extension_id)
    local_name = normalize_completion_handler_name(handler_name)
    if not callable(handler):
        raise ValueError('completion handler must be callable')
    if not isinstance(replace, bool):
        raise ValueError('replace must be bool')
    key = completion_handler_key(ext_id, local_name)
    if key in EXTENSION_COMPLETION_HANDLERS and not replace:
        raise ValueError(f"extension completion handler '{key}' is already registered")
    EXTENSION_COMPLETION_HANDLERS[key] = handler
    return key


def remove_extension_completion_handlers(extension_id: str, keys: set[str]) -> None:
    """Remove only handlers recorded for one unloaded extension."""
    ext_id = normalize_extension_id(extension_id)
    prefix = f'{ext_id}.'
    for key in set(keys):
        if str(key).startswith(prefix):
            EXTENSION_COMPLETION_HANDLERS.pop(str(key), None)


def is_extension_completion_handler_registered(
    extension_id: str,
    handler_name: str,
) -> bool:
    """Return whether the exact owner-local handler is currently available."""
    return completion_handler_key(extension_id, handler_name) in EXTENSION_COMPLETION_HANDLERS


async def dispatch_extension_completion(
    extension_id: str,
    handler_name: str,
    context: Mapping[str, Any],
    *,
    bot: Any = None,
) -> dict[str, Any]:
    """Invoke a registered completion handler and normalize its acknowledgement."""
    if not isinstance(context, Mapping):
        raise ValueError('completion context must be a mapping')
    key = completion_handler_key(extension_id, handler_name)
    handler = EXTENSION_COMPLETION_HANDLERS.get(key)
    if handler is None:
        raise LookupError(f"extension completion handler '{key}' is unavailable")

    from bot.utils.custom_extensions import _extension_bot_context

    with _extension_bot_context(bot):
        if inspect.iscoroutinefunction(handler):
            raw_result = handler(dict(context))
        else:
            raw_result = await asyncio.to_thread(handler, dict(context))
        if inspect.isawaitable(raw_result):
            raw_result = await raw_result
    return normalize_extension_completion_result(raw_result)


def normalize_extension_completion_result(raw_result: Any) -> dict[str, Any]:
    """Validate the small acknowledgement contract of a durable delivery."""
    if raw_result is None:
        return {'ok': True, 'retry': False}
    if not isinstance(raw_result, Mapping):
        raise ValueError('completion handler must return a mapping or None')
    result = dict(raw_result)
    unknown = set(result) - {'ok', 'retry', 'reason'}
    if unknown:
        raise ValueError(
            f"unsupported completion result fields: {', '.join(sorted(unknown))}"
        )
    if 'ok' in result and not isinstance(result['ok'], bool):
        raise ValueError('completion result ok must be bool')
    if 'retry' in result and not isinstance(result['retry'], bool):
        raise ValueError('completion result retry must be bool')
    if 'reason' in result:
        if not isinstance(result['reason'], str):
            raise ValueError('completion result reason must be a string')
        result['reason'] = result['reason'].strip()[:1000]
    result.setdefault('ok', True)
    if result['ok']:
        if result.get('retry'):
            raise ValueError('successful completion cannot request retry')
        result['retry'] = False
    else:
        result.setdefault('retry', True)
    return result


def completion_handler_key(extension_id: str, handler_name: str) -> str:
    """Build the internal owner-qualified registry key."""
    return (
        f'{normalize_extension_id(extension_id)}.'
        f'{normalize_completion_handler_name(handler_name)}'
    )


__all__ = [
    'EXTENSION_COMPLETION_HANDLERS',
    'completion_handler_key',
    'dispatch_extension_completion',
    'is_extension_completion_handler_registered',
    'normalize_extension_completion_result',
    'register_extension_completion_handler',
    'remove_extension_completion_handlers',
]
