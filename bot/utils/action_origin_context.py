"""Validated extension-owned context carried through one core action workflow."""
from __future__ import annotations

import json
import math
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from database.db_extensions import normalize_extension_id

ORIGIN_CONTEXT_VERSION = 1
ORIGIN_CONTEXT_MAX_JSON_BYTES = 2048
ORIGIN_CONTEXT_MAX_DEPTH = 4
ORIGIN_CONTEXT_MAX_NODES = 128
ORIGIN_CONTEXT_MAX_KEY_LENGTH = 64
ORIGIN_CONTEXT_MAX_STRING_LENGTH = 512

_HANDLER_RE = re.compile(r'^[a-z][a-z0-9_]{0,31}$')
_WORKFLOW_RE = re.compile(r'^[A-Za-z0-9_-]{16,64}$')


@dataclass(frozen=True)
class ActionOriginContext:
    """Core-owned immutable view of an extension context snapshot."""

    owner_extension_id: str
    schema_version: int
    payload: Mapping[str, Any]
    workflow_id: str
    completion_handler: str | None = None

    def as_storage_dict(self) -> dict[str, Any]:
        """Return the exact trusted fields persisted outside action params."""
        return {
            'owner_extension_id': self.owner_extension_id,
            'schema_version': self.schema_version,
            'payload': dict(self.payload),
            'workflow_id': self.workflow_id,
            'completion_handler': self.completion_handler,
        }

    def as_public_envelope(self) -> dict[str, Any]:
        """Return the stable extension-facing envelope without core ownership fields."""
        result: dict[str, Any] = {
            'version': self.schema_version,
            'payload': dict(self.payload),
        }
        if self.completion_handler is not None:
            result['handler'] = self.completion_handler
        return result


def normalize_public_origin_context(
    value: Any,
    *,
    owner_extension_id: str,
    workflow_id: str | None = None,
) -> ActionOriginContext:
    """Validate the public ``version/handler/payload`` envelope and inject ownership."""
    raw = normalize_public_origin_context_envelope(value)
    return ActionOriginContext(
        owner_extension_id=normalize_extension_id(owner_extension_id),
        schema_version=raw['version'],
        payload=MappingProxyType(raw['payload']),
        workflow_id=normalize_origin_workflow_id(workflow_id or new_origin_workflow_id()),
        completion_handler=raw.get('handler'),
    )


def normalize_public_origin_context_envelope(value: Any) -> dict[str, Any]:
    """Validate and detach the extension-facing origin context envelope."""
    if not isinstance(value, Mapping):
        raise ValueError('origin_context must be a mapping')
    raw = dict(value)
    unknown = set(raw) - {'version', 'handler', 'payload'}
    if unknown:
        raise ValueError(
            f"unsupported origin_context fields: {', '.join(sorted(unknown))}"
        )
    if 'version' not in raw:
        raise ValueError('origin_context.version is required')
    version = _positive_int(raw.get('version'), 'origin_context.version')
    if version != ORIGIN_CONTEXT_VERSION:
        raise ValueError(f'unsupported origin_context version: {version}')
    if 'payload' not in raw:
        raise ValueError('origin_context.payload is required')
    payload = normalize_origin_payload(raw.get('payload'))
    handler = normalize_completion_handler_name(raw.get('handler'), optional=True)
    result: dict[str, Any] = {'version': version, 'payload': payload}
    if handler is not None:
        result['handler'] = handler
    return result


def normalize_stored_origin_context(value: Any) -> ActionOriginContext | None:
    """Validate a trusted storage-shaped snapshot, returning ``None`` when absent."""
    if value is None:
        return None
    if isinstance(value, ActionOriginContext):
        return value
    if not isinstance(value, Mapping):
        raise ValueError('stored origin context must be a mapping')
    raw = dict(value)
    if not any(_has_value(raw.get(field)) for field in (
        'owner_extension_id',
        'schema_version',
        'workflow_id',
        'completion_handler',
    )) and not raw.get('payload'):
        return None
    unknown = set(raw) - {
        'owner_extension_id',
        'schema_version',
        'payload',
        'workflow_id',
        'completion_handler',
    }
    if unknown:
        raise ValueError(
            f"unsupported stored origin context fields: {', '.join(sorted(unknown))}"
        )
    version = _positive_int(raw.get('schema_version'), 'origin_context.schema_version')
    if version != ORIGIN_CONTEXT_VERSION:
        raise ValueError(f'unsupported origin_context version: {version}')
    return ActionOriginContext(
        owner_extension_id=normalize_extension_id(raw.get('owner_extension_id')),
        schema_version=version,
        payload=MappingProxyType(normalize_origin_payload(raw.get('payload'))),
        workflow_id=normalize_origin_workflow_id(raw.get('workflow_id')),
        completion_handler=normalize_completion_handler_name(
            raw.get('completion_handler'),
            optional=True,
        ),
    )


def normalize_origin_payload(value: Any) -> dict[str, Any]:
    """Return a detached, bounded JSON object accepted at the public boundary."""
    if not isinstance(value, Mapping):
        raise ValueError('origin_context.payload must be a mapping')
    nodes = [0]
    normalized = _normalize_json_value(dict(value), depth=0, nodes=nodes)
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        separators=(',', ':'),
        sort_keys=True,
    ).encode('utf-8')
    if len(encoded) > ORIGIN_CONTEXT_MAX_JSON_BYTES:
        raise ValueError(
            f'origin_context.payload exceeds {ORIGIN_CONTEXT_MAX_JSON_BYTES} UTF-8 bytes'
        )
    return normalized


def encode_origin_payload(value: Mapping[str, Any]) -> str:
    """Encode an already validated payload in canonical form for persistence."""
    normalized = normalize_origin_payload(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        separators=(',', ':'),
        sort_keys=True,
    )


def decode_origin_payload(value: Any) -> dict[str, Any]:
    """Decode and validate a persisted origin payload."""
    if value is None or value == '':
        return {}
    if not isinstance(value, str):
        raise ValueError('stored origin_context payload must be JSON text')
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError('stored origin_context payload is invalid JSON') from exc
    return normalize_origin_payload(decoded)


def normalize_completion_handler_name(value: Any, *, optional: bool = False) -> str | None:
    """Normalize one extension-local durable completion handler name."""
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError('origin_context.handler must be a string')
    normalized = value.strip().casefold()
    if not _HANDLER_RE.fullmatch(normalized):
        raise ValueError('origin_context.handler must match ^[a-z][a-z0-9_]{0,31}$')
    return normalized


def normalize_origin_workflow_id(value: Any) -> str:
    """Validate a compact opaque workflow id generated by core."""
    if not isinstance(value, str):
        raise ValueError('origin workflow_id must be a string')
    normalized = value.strip()
    if not _WORKFLOW_RE.fullmatch(normalized):
        raise ValueError('origin workflow_id is invalid')
    return normalized


def new_origin_workflow_id() -> str:
    """Generate a compact URL-safe workflow id with sufficient entropy."""
    return secrets.token_urlsafe(18)


def _normalize_json_value(value: Any, *, depth: int, nodes: list[int]) -> Any:
    if depth > ORIGIN_CONTEXT_MAX_DEPTH:
        raise ValueError(f'origin_context.payload depth exceeds {ORIGIN_CONTEXT_MAX_DEPTH}')
    nodes[0] += 1
    if nodes[0] > ORIGIN_CONTEXT_MAX_NODES:
        raise ValueError(f'origin_context.payload exceeds {ORIGIN_CONTEXT_MAX_NODES} nodes')
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError('origin_context.payload numbers must be finite')
        return value
    if isinstance(value, str):
        if len(value) > ORIGIN_CONTEXT_MAX_STRING_LENGTH:
            raise ValueError(
                f'origin_context.payload strings must not exceed '
                f'{ORIGIN_CONTEXT_MAX_STRING_LENGTH} characters'
            )
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError('origin_context.payload object keys must be strings')
            if not key or len(key) > ORIGIN_CONTEXT_MAX_KEY_LENGTH:
                raise ValueError(
                    f'origin_context.payload keys must contain 1-'
                    f'{ORIGIN_CONTEXT_MAX_KEY_LENGTH} characters'
                )
            result[key] = _normalize_json_value(item, depth=depth + 1, nodes=nodes)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _normalize_json_value(item, depth=depth + 1, nodes=nodes)
            for item in value
        ]
    raise ValueError('origin_context.payload contains a non-JSON value')


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f'{field} must be a positive integer')
    return value


def _has_value(value: Any) -> bool:
    return value is not None and value != ''


__all__ = [
    'ActionOriginContext',
    'ORIGIN_CONTEXT_MAX_JSON_BYTES',
    'ORIGIN_CONTEXT_VERSION',
    'decode_origin_payload',
    'encode_origin_payload',
    'new_origin_workflow_id',
    'normalize_completion_handler_name',
    'normalize_origin_payload',
    'normalize_origin_workflow_id',
    'normalize_public_origin_context',
    'normalize_public_origin_context_envelope',
    'normalize_stored_origin_context',
]
