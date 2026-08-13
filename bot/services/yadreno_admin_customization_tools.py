"""Typed, bounded customization operations exposed to Yadreno Admin."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

from bot.utils.custom_extensions import (
    CUSTOM_EXTENSIONS_DIR,
    CUSTOM_EXTENSIONS_ENABLED_SETTING,
    get_custom_extensions_diagnostics,
    validate_custom_extension_file,
)
from bot.utils.page_renderer import get_page_stored_data
from bot.utils.user_ui_texts import (
    reload_user_ui_text_cache,
    validate_user_ui_text_custom,
)
from database.requests import (
    apply_page_custom_patch,
    clear_user_ui_text_custom,
    create_bot_database_backup,
    create_trial_offer,
    delete_trial_offer,
    get_all_groups,
    get_all_tariffs,
    get_all_trial_offers,
    get_all_user_ui_texts,
    get_page,
    get_page_keys,
    get_setting,
    get_tariff_by_id,
    get_trial_offer_by_id,
    get_trial_usage_scope,
    get_user_ui_text,
    is_trial_offer_storage_ready,
    normalize_page_custom_patch,
    set_setting,
    set_trial_usage_scope,
    set_user_ui_text_custom,
    update_trial_offer,
)


CUSTOMIZATION_TOOL_NAMES = frozenset({
    'satellite_customization_inspect',
    'satellite_customization_apply',
})
INSPECT_SCOPES = frozenset({
    'overview',
    'page',
    'ui_texts',
    'settings',
    'trial_offers',
    'extensions',
})
APPLY_OPERATIONS = frozenset({
    'page.update',
    'ui_text.set',
    'setting.set',
    'trial.scope.set',
    'trial.offer.create',
    'trial.offer.update',
    'trial.offer.delete',
    'extensions.loader.set',
})
CUSTOM_SETTING_KEYS = (
    'key_name_prefix',
    'my_keys_item_template',
    'notification_text',
    'traffic_notification_text',
    'referral_new_ref_notification_text',
    'referral_purchase_notification_text',
)
_CUSTOM_SETTING_KEY_SET = frozenset(CUSTOM_SETTING_KEYS)
_TRIAL_USAGE_SCOPES = frozenset({'once_per_user', 'once_per_group'})
_APPLY_ARGUMENTS = {
    'page.update': frozenset({'operation', 'page_key', 'page_patch'}),
    'ui_text.set': frozenset({'operation', 'text_key', 'value'}),
    'setting.set': frozenset({'operation', 'setting_key', 'value'}),
    'trial.scope.set': frozenset({'operation', 'scope'}),
    'trial.offer.create': frozenset({'operation', 'tariff_id', 'enabled'}),
    'trial.offer.update': frozenset({
        'operation', 'offer_id', 'tariff_id', 'enabled',
    }),
    'trial.offer.delete': frozenset({'operation', 'offer_id'}),
    'extensions.loader.set': frozenset({
        'operation',
        'enabled',
        'expected_extension_files',
        'confirm_all_existing_extensions',
    }),
}
_MAX_RESULT_CHARS = 12_000
_MAX_STATE_STRING_CHARS = 8_000


def _json_result(payload: dict[str, Any]) -> str:
    truncated = [False]

    def bound(value: Any, string_limit: int) -> Any:
        if isinstance(value, str) and len(value) > string_limit:
            truncated[0] = True
            return value[:string_limit] + '…'
        if isinstance(value, dict):
            return {str(key): bound(item, string_limit) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [bound(item, string_limit) for item in value]
        return value

    bounded = bound(payload, _MAX_STATE_STRING_CHARS)
    if truncated[0]:
        bounded['content_truncated'] = True
    rendered = json.dumps(
        bounded,
        ensure_ascii=False,
        separators=(',', ':'),
        default=str,
    )
    if len(rendered) <= _MAX_RESULT_CHARS:
        return rendered
    bounded = bound(payload, 1_000)
    bounded['content_truncated'] = True
    rendered = json.dumps(
        bounded,
        ensure_ascii=False,
        separators=(',', ':'),
        default=str,
    )
    if len(rendered) <= _MAX_RESULT_CHARS:
        return rendered
    return json.dumps({
        'status': 'error',
        'error': 'bounded result still exceeds the response limit; use a smaller page',
    }, separators=(',', ':'))


def _error(message: str) -> str:
    return _json_result({'status': 'error', 'error': message})


def _normalize_pagination(args: dict[str, Any]) -> tuple[int, int]:
    cursor = args.get('cursor', 0)
    limit = args.get('limit', 20)
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
        raise ValueError('cursor must be a non-negative integer')
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError('limit must be an integer between 1 and 50')
    return cursor, limit


def _paginated(
    scope: str,
    items: list[dict[str, Any]],
    cursor: int,
    limit: int,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    page = items[cursor:cursor + limit]
    next_cursor = cursor + len(page)
    result: dict[str, Any] = {
        'status': 'ok',
        'scope': scope,
        'cursor': cursor,
        'limit': limit,
        'total': len(items),
        'next_cursor': next_cursor if next_cursor < len(items) else None,
        'items': page,
    }
    if extra:
        result.update(extra)
    return result


def _page_state(page_key: str) -> dict[str, Any] | None:
    row = get_page(page_key)
    if row is None:
        return None
    return {
        'page_key': str(row['page_key']),
        'custom': {
            'text_custom': row.get('text_custom'),
            'image_custom': row.get('image_custom'),
            'media_type_custom': row.get('media_type_custom'),
            'buttons_custom': _decode_json_or_raw(row.get('buttons_custom')),
        },
        'effective': get_page_stored_data(page_key),
        'updated_at': row.get('updated_at'),
    }


def _decode_json_or_raw(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _inspect_pages(key: str | None, cursor: int, limit: int) -> dict[str, Any]:
    if key:
        state = _page_state(key)
        if state is None:
            raise KeyError(f'unknown page_key: {key}')
        return {'status': 'ok', 'scope': 'page', 'item': state}
    items = []
    for page_key in sorted(get_page_keys()):
        row = get_page(page_key) or {}
        items.append({
            'page_key': page_key,
            'has_custom_text': row.get('text_custom') is not None,
            'has_custom_image': row.get('image_custom') is not None,
            'has_custom_buttons': row.get('buttons_custom') is not None,
            'updated_at': row.get('updated_at'),
        })
    return _paginated('page', items, cursor, limit)


def _ui_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        'text_key': row.get('text_key'),
        'text_default': row.get('text_default'),
        'text_custom': row.get('text_custom'),
        'text_effective': row.get('text_effective'),
        'text_format': row.get('text_format'),
        'updated_at': row.get('updated_at'),
    }


def _inspect_ui_texts(key: str | None, cursor: int, limit: int) -> dict[str, Any]:
    if key:
        row = get_user_ui_text(key)
        if row is None:
            raise KeyError(f'unknown text_key: {key}')
        return {'status': 'ok', 'scope': 'ui_texts', 'item': _ui_row(row)}
    rows = [_ui_row(row) for row in get_all_user_ui_texts()]
    return _paginated('ui_texts', rows, cursor, limit)


def _inspect_settings(key: str | None, cursor: int, limit: int) -> dict[str, Any]:
    if key and key not in _CUSTOM_SETTING_KEY_SET:
        raise KeyError(f'setting is not customizable through this tool: {key}')
    keys = [key] if key else list(CUSTOM_SETTING_KEYS)
    rows = [{'setting_key': item, 'value': get_setting(item)} for item in keys]
    return _paginated('settings', rows, cursor, limit)


def _trial_offer_state(offer: dict[str, Any]) -> dict[str, Any]:
    offer_id = offer.get('offer_id')
    return {
        'offer_id': offer_id,
        'tariff_id': offer.get('tariff_id'),
        'tariff_name': offer.get('tariff_name'),
        'group_id': offer.get('group_id'),
        'group_name': offer.get('group_name'),
        'is_primary': bool(offer.get('is_primary')),
        'is_enabled': bool(offer.get('is_enabled')),
        'tariff_is_active': bool(offer.get('tariff_is_active')),
        'duration_days': int(offer.get('duration_days') or 0),
        'traffic_limit_gb': int(offer.get('traffic_limit_gb') or 0),
        'max_ips': int(offer.get('max_ips') or 1),
        'updated_at': offer.get('updated_at'),
        'action_type': 'internal',
        'action_value': (
            f'cmd_trial_offer:{int(offer_id)}'
            if offer_id is not None
            else None
        ),
    }


def _inspect_trial_offers(key: str | None, cursor: int, limit: int) -> dict[str, Any]:
    if not is_trial_offer_storage_ready():
        return {
            'status': 'unavailable',
            'scope': 'trial_offers',
            'reason': 'database_schema_not_ready',
        }
    if key == 'eligible_tariffs':
        group_names = {
            int(row['id']): row.get('name')
            for row in get_all_groups()
        }
        tariffs = [
            {
                'tariff_id': int(row['id']),
                'name': row.get('name'),
                'group_id': row.get('group_id'),
                'group_name': group_names.get(int(row.get('group_id') or 1)),
                'is_active': bool(row.get('is_active')),
                'duration_days': row.get('duration_days'),
                'traffic_limit_gb': row.get('traffic_limit_gb'),
                'max_ips': row.get('max_ips'),
            }
            for row in get_all_tariffs(include_hidden=True)
        ]
        return _paginated(
            'trial_offers',
            tariffs,
            cursor,
            limit,
            extra={'view': 'eligible_tariffs'},
        )
    if key:
        try:
            offer_id = int(key)
        except ValueError as exc:
            raise ValueError('trial offer key must be an offer id') from exc
        offer = get_trial_offer_by_id(offer_id)
        if offer is None:
            raise KeyError(f'unknown trial offer: {offer_id}')
        return {
            'status': 'ok',
            'scope': 'trial_offers',
            'usage_scope': get_trial_usage_scope(),
            'item': _trial_offer_state(offer),
        }
    offers = [_trial_offer_state(item) for item in get_all_trial_offers()]
    return _paginated(
        'trial_offers',
        offers,
        cursor,
        limit,
        extra={'usage_scope': get_trial_usage_scope()},
    )


def _extension_file_states(diagnostics: dict[str, Any]) -> list[dict[str, Any]]:
    loaded = set(diagnostics.get('last_load', {}).get('loaded') or [])
    failed = diagnostics.get('last_load', {}).get('failed') or {}
    registrations = diagnostics.get('registrations') or {}
    result = []
    for item in diagnostics.get('files') or []:
        name = str(item.get('file') or '')
        extension_id = str(item.get('extension') or Path(name).stem)
        validation = (
            validate_custom_extension_file(CUSTOM_EXTENSIONS_DIR / name)
            if item.get('status') == 'candidate'
            else {'ok': None, 'error': ''}
        )
        result.append({
            'file': name,
            'extension': extension_id,
            'status': item.get('status'),
            'validation_ok': validation.get('ok'),
            'validation_error': validation.get('error') or None,
            'loaded': extension_id in loaded,
            'load_error': failed.get(name),
            'registrations': registrations.get(extension_id, {}),
        })
    return result


def _inspect_extensions(cursor: int, limit: int) -> dict[str, Any]:
    diagnostics = get_custom_extensions_diagnostics()
    last_load = diagnostics.get('last_load') or {}
    loader = {
        'configured_value': diagnostics.get('configured_value'),
        'configured_enabled': diagnostics.get('configured_enabled'),
        'runtime_loader_enabled': diagnostics.get('runtime_loader_enabled'),
        'restart_required': diagnostics.get('restart_required'),
        'directory_status': diagnostics.get('directory_status'),
        'last_load': {
            'loaded_count': len(last_load.get('loaded') or []),
            'failed_count': len(last_load.get('failed') or {}),
            'skipped': bool(last_load.get('skipped')),
            'reason': last_load.get('reason') or '',
            'loader_enabled': bool(last_load.get('loader_enabled')),
            'directory_fingerprint': last_load.get('directory_fingerprint') or '',
        },
    }
    return _paginated(
        'extensions',
        _extension_file_states(diagnostics),
        cursor,
        limit,
        extra={'loader': loader},
    )


def inspect_customization(args: dict[str, Any]) -> str:
    """Return one bounded read-only customization snapshot."""
    scope = str(args.get('scope') or '')
    if scope not in INSPECT_SCOPES:
        return _error('unsupported customization inspect scope')
    unknown = sorted(set(args) - {'scope', 'key', 'cursor', 'limit'})
    if unknown:
        return _error(f'unknown inspect arguments: {unknown}')
    try:
        cursor, limit = _normalize_pagination(args)
        key_value = args.get('key')
        key = str(key_value).strip() if key_value is not None else None
        inspectors: dict[str, Callable[[], dict[str, Any]]] = {
            'page': lambda: _inspect_pages(key, cursor, limit),
            'ui_texts': lambda: _inspect_ui_texts(key, cursor, limit),
            'settings': lambda: _inspect_settings(key, cursor, limit),
            'trial_offers': lambda: _inspect_trial_offers(key, cursor, limit),
            'extensions': lambda: _inspect_extensions(cursor, limit),
        }
        if scope == 'overview':
            extension_state = get_custom_extensions_diagnostics()
            payload = {
                'status': 'ok',
                'scope': 'overview',
                'counts': {
                    'pages': len(get_page_keys()),
                    'ui_texts': len(get_all_user_ui_texts()),
                    'settings': len(CUSTOM_SETTING_KEYS),
                    'trial_offers': (
                        len(get_all_trial_offers())
                        if is_trial_offer_storage_ready()
                        else None
                    ),
                    'extension_files': len(extension_state.get('files') or []),
                },
                'trial_storage_ready': is_trial_offer_storage_ready(),
                'extensions': {
                    'configured_value': extension_state.get('configured_value'),
                    'configured_enabled': extension_state.get('configured_enabled'),
                    'runtime_loader_enabled': extension_state.get('runtime_loader_enabled'),
                    'restart_required': extension_state.get('restart_required'),
                    'directory_status': extension_state.get('directory_status'),
                },
            }
        else:
            payload = inspectors[scope]()
        return _json_result(payload)
    except (KeyError, TypeError, ValueError, OSError, sqlite3.Error) as exc:
        return _error(str(exc))


def _unchanged(
    operation: str,
    read_back: Any,
    *,
    restart_required: bool = False,
) -> str:
    return _json_result({
        'status': 'ok',
        'operation': operation,
        'changed': False,
        'backup_path': None,
        'restart_required': restart_required,
        'read_back': read_back,
    })


def _changed(
    operation: str,
    backup_path: str,
    read_back: Any,
    *,
    restart_required: bool = False,
) -> str:
    return _json_result({
        'status': 'ok',
        'operation': operation,
        'changed': True,
        'backup_path': backup_path,
        'restart_required': restart_required,
        'read_back': read_back,
    })


def _apply_page(args: dict[str, Any]) -> str:
    page_key = str(args.get('page_key') or '').strip()
    patch = args.get('page_patch')
    if not page_key or not isinstance(patch, dict) or not patch:
        raise ValueError('page_key and non-empty page_patch are required')
    before = get_page(page_key)
    if before is None:
        raise KeyError(f'unknown page_key: {page_key}')
    comparable_patch = normalize_page_custom_patch(patch)
    if all(before.get(field) == value for field, value in comparable_patch.items()):
        return _unchanged('page.update', _page_state(page_key))
    backup_path = create_bot_database_backup()
    apply_page_custom_patch(page_key, patch)
    after = get_page(page_key)
    if after is None or any(
        after.get(field) != value for field, value in comparable_patch.items()
    ):
        raise RuntimeError('page read-back mismatch')
    return _changed('page.update', backup_path, _page_state(page_key))


def _apply_ui_text(args: dict[str, Any]) -> str:
    text_key = str(args.get('text_key') or '').strip()
    if not text_key or 'value' not in args:
        raise ValueError('text_key and value are required')
    value = args.get('value')
    validate_user_ui_text_custom(text_key, value)
    before = get_user_ui_text(text_key)
    if before is None:
        raise KeyError(f'unknown text_key: {text_key}')
    previous = before.get('text_custom')
    if previous == value:
        return _unchanged('ui_text.set', _ui_row(before))
    backup_path = create_bot_database_backup()
    changed = (
        clear_user_ui_text_custom(text_key)
        if value is None
        else set_user_ui_text_custom(text_key, value)
    )
    if not changed:
        raise RuntimeError('UI text mutation did not update an existing row')
    after = get_user_ui_text(text_key)
    if after is None or after.get('text_custom') != value:
        if previous is None:
            clear_user_ui_text_custom(text_key)
        else:
            set_user_ui_text_custom(text_key, str(previous))
        raise RuntimeError('UI text read-back mismatch')
    try:
        reload_user_ui_text_cache()
    except RuntimeError:
        if previous is None:
            clear_user_ui_text_custom(text_key)
        else:
            set_user_ui_text_custom(text_key, str(previous))
        reload_user_ui_text_cache()
        raise
    return _changed(
        'ui_text.set',
        backup_path,
        _ui_row(after),
    )


def _validate_setting_value(key: str, value: Any) -> str:
    if key not in _CUSTOM_SETTING_KEY_SET:
        raise ValueError(f'setting is not allowlisted: {key}')
    if not isinstance(value, str):
        raise TypeError('setting value must be a string')
    if not value.strip():
        raise ValueError('setting value must not be empty')
    if len(value) > 50_000:
        raise ValueError('setting value is too large')
    if key == 'key_name_prefix':
        if '\n' in value or '\r' in value:
            raise ValueError('key_name_prefix must be a single line')
        if len(value.strip()) > 30:
            raise ValueError('key_name_prefix exceeds the key-name limit')
        return value.strip()
    return value


def _apply_setting(args: dict[str, Any]) -> str:
    key = str(args.get('setting_key') or '').strip()
    if 'value' not in args:
        raise ValueError('setting_key and value are required')
    value = _validate_setting_value(key, args.get('value'))
    before = get_setting(key)
    if before == value:
        return _unchanged('setting.set', {'setting_key': key, 'value': before})
    backup_path = create_bot_database_backup()
    set_setting(key, value)
    read_back = get_setting(key)
    if read_back != value:
        raise RuntimeError('setting read-back mismatch')
    return _changed(
        'setting.set',
        backup_path,
        {'setting_key': key, 'value': read_back},
    )


def _apply_trial_scope(args: dict[str, Any]) -> str:
    scope = str(args.get('scope') or '').strip()
    if scope not in _TRIAL_USAGE_SCOPES:
        raise ValueError('scope must be once_per_user or once_per_group')
    before = get_trial_usage_scope()
    if before == scope:
        return _unchanged('trial.scope.set', {'scope': before})
    backup_path = create_bot_database_backup()
    set_trial_usage_scope(scope)
    read_back = get_trial_usage_scope()
    if read_back != scope:
        raise RuntimeError('trial scope read-back mismatch')
    return _changed('trial.scope.set', backup_path, {'scope': read_back})


def _apply_trial_offer(operation: str, args: dict[str, Any]) -> str:
    if not is_trial_offer_storage_ready():
        raise RuntimeError('trial offer database schema is not ready')
    if operation == 'trial.offer.create':
        tariff_id = args.get('tariff_id')
        if isinstance(tariff_id, bool) or not isinstance(tariff_id, int):
            raise ValueError('tariff_id is required')
        _validate_trial_tariff(tariff_id)
        enabled = args.get('enabled', True)
        if not isinstance(enabled, bool):
            raise TypeError('enabled must be bool')
        backup_path = create_bot_database_backup()
        offer_id = create_trial_offer(tariff_id, enabled=enabled)
        after = get_trial_offer_by_id(offer_id)
        if (
            after is None
            or int(after.get('tariff_id') or 0) != tariff_id
            or bool(after.get('is_enabled')) != enabled
        ):
            raise RuntimeError('trial offer read-back mismatch')
        return _changed(operation, backup_path, _trial_offer_state(after))

    offer_id = args.get('offer_id')
    if isinstance(offer_id, bool) or not isinstance(offer_id, int):
        raise ValueError('offer_id is required')
    before = get_trial_offer_by_id(offer_id)
    if before is None:
        raise KeyError(f'unknown trial offer: {offer_id}')
    if bool(before.get('is_primary')):
        raise ValueError('the primary trial offer is protected')
    if operation == 'trial.offer.delete':
        backup_path = create_bot_database_backup()
        if not delete_trial_offer(offer_id):
            raise RuntimeError('trial offer was not deleted')
        if get_trial_offer_by_id(offer_id) is not None:
            raise RuntimeError('trial offer delete read-back mismatch')
        return _changed(operation, backup_path, {'offer_id': offer_id, 'exists': False})

    tariff_id = args.get('tariff_id')
    enabled = args.get('enabled')
    if tariff_id is None and enabled is None:
        raise ValueError('tariff_id or enabled is required')
    if tariff_id is not None and (isinstance(tariff_id, bool) or not isinstance(tariff_id, int)):
        raise TypeError('tariff_id must be an integer')
    if tariff_id is not None:
        _validate_trial_tariff(tariff_id)
    if enabled is not None and not isinstance(enabled, bool):
        raise TypeError('enabled must be bool')
    unchanged = (
        (tariff_id is None or int(before['tariff_id']) == tariff_id)
        and (enabled is None or bool(before['is_enabled']) == enabled)
    )
    if unchanged:
        return _unchanged(operation, _trial_offer_state(before))
    backup_path = create_bot_database_backup()
    if not update_trial_offer(offer_id, tariff_id=tariff_id, enabled=enabled):
        raise RuntimeError('trial offer was not updated')
    after = get_trial_offer_by_id(offer_id)
    if (
        after is None
        or (tariff_id is not None and int(after.get('tariff_id') or 0) != tariff_id)
        or (enabled is not None and bool(after.get('is_enabled')) != enabled)
    ):
        raise RuntimeError('trial offer read-back mismatch')
    return _changed(operation, backup_path, _trial_offer_state(after))


def _validate_trial_tariff(tariff_id: int) -> None:
    tariff = get_tariff_by_id(int(tariff_id))
    if tariff is None:
        raise ValueError('trial tariff does not exist')
    if tariff.get('system_type') is not None:
        raise ValueError('system tariffs cannot be used for trial offers')


def _extension_validation() -> dict[str, Any]:
    if not CUSTOM_EXTENSIONS_DIR.exists():
        return {
            'ok': True,
            'directory_status': 'directory_missing',
            'files': [],
        }
    if not CUSTOM_EXTENSIONS_DIR.is_dir():
        return {'ok': False, 'directory_status': 'not_directory', 'files': []}
    files = [
        validate_custom_extension_file(path)
        for path in sorted(CUSTOM_EXTENSIONS_DIR.glob('*.py'))
        if not path.name.startswith('_')
    ]
    return {
        'ok': all(item.get('ok') for item in files),
        'directory_status': 'ok',
        'files': files,
    }


def _expected_extension_files(args: dict[str, Any]) -> set[str]:
    raw = args.get('expected_extension_files') or []
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise TypeError('expected_extension_files must be an array of filenames')
    normalized = {item.strip() for item in raw if item.strip()}
    if any(Path(item).name != item or not item.endswith('.py') for item in normalized):
        raise ValueError('expected_extension_files must contain plain .py filenames')
    return normalized


def _apply_extension_loader(args: dict[str, Any]) -> str:
    enabled = args.get('enabled')
    if not isinstance(enabled, bool):
        raise TypeError('enabled is required and must be bool')
    confirmed = args.get('confirm_all_existing_extensions', False)
    if not isinstance(confirmed, bool):
        raise TypeError('confirm_all_existing_extensions must be bool')
    diagnostics_before = get_custom_extensions_diagnostics()
    configured_before = bool(diagnostics_before.get('configured_enabled'))
    if configured_before == enabled:
        restart_required = bool(diagnostics_before.get('restart_required'))
        return _unchanged(
            'extensions.loader.set',
            {
                'configured_value': diagnostics_before.get('configured_value'),
                'configured_enabled': configured_before,
                'runtime_loader_enabled': diagnostics_before.get('runtime_loader_enabled'),
                'restart_required': restart_required,
            },
            restart_required=restart_required,
        )

    validation: dict[str, Any] | None = None
    if enabled:
        validation = _extension_validation()
        if not validation['ok']:
            return _json_result({
                'status': 'validation_failed',
                'operation': 'extensions.loader.set',
                'changed': False,
                'backup_path': None,
                'restart_required': False,
                'validation': validation,
            })
        expected = _expected_extension_files(args)
        existing = {
            str(item.get('file') or '')
            for item in validation.get('files') or []
            if item.get('file')
        }
        unrelated = sorted(existing - expected)
        if unrelated and not confirmed:
            return _json_result({
                'status': 'confirmation_required',
                'operation': 'extensions.loader.set',
                'changed': False,
                'backup_path': None,
                'restart_required': False,
                'unrelated_extension_files': unrelated,
                'validation': validation,
            })

    backup_path = create_bot_database_backup()
    set_setting(CUSTOM_EXTENSIONS_ENABLED_SETTING, '1' if enabled else '0')
    diagnostics_after = get_custom_extensions_diagnostics()
    if bool(diagnostics_after.get('configured_enabled')) != enabled:
        raise RuntimeError('extension loader setting read-back mismatch')
    return _changed(
        'extensions.loader.set',
        backup_path,
        {
            'configured_value': diagnostics_after.get('configured_value'),
            'configured_enabled': diagnostics_after.get('configured_enabled'),
            'runtime_loader_enabled': diagnostics_after.get('runtime_loader_enabled'),
            'restart_required': diagnostics_after.get('restart_required'),
            'validation': validation,
        },
        restart_required=bool(diagnostics_after.get('restart_required')),
    )


def apply_customization(args: dict[str, Any]) -> str:
    """Apply one allowlisted mutation with backup and read-back."""
    operation = str(args.get('operation') or '')
    if operation not in APPLY_OPERATIONS:
        return _error('unsupported customization apply operation')
    unknown = sorted(set(args) - _APPLY_ARGUMENTS[operation])
    if unknown:
        return _error(f'unknown arguments for {operation}: {unknown}')
    handlers: dict[str, Callable[[], str]] = {
        'page.update': lambda: _apply_page(args),
        'ui_text.set': lambda: _apply_ui_text(args),
        'setting.set': lambda: _apply_setting(args),
        'trial.scope.set': lambda: _apply_trial_scope(args),
        'trial.offer.create': lambda: _apply_trial_offer(operation, args),
        'trial.offer.update': lambda: _apply_trial_offer(operation, args),
        'trial.offer.delete': lambda: _apply_trial_offer(operation, args),
        'extensions.loader.set': lambda: _apply_extension_loader(args),
    }
    try:
        return handlers[operation]()
    except (KeyError, TypeError, ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        return _error(str(exc))


def execute_customization_tool(tool: str, args: dict[str, Any]) -> str:
    """Dispatch one typed customization tool without a generic fallback."""
    if not isinstance(args, dict):
        return _error('tool arguments must be an object')
    if tool == 'satellite_customization_inspect':
        return inspect_customization(args)
    if tool == 'satellite_customization_apply':
        return apply_customization(args)
    return _error(f'unknown typed customization tool: {tool}')
