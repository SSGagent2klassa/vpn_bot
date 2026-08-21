"""Reconcile durable key-subscription composition with panel External Links."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import Counter
from typing import Any, Iterable, Mapping

from bot.services import vpn_api
from bot.services.panel_sync_coordinator import regular_panel_operation
from bot.services.panels.base import PanelErrorKind, PanelRequestError
from database import requests as db


logger = logging.getLogger(__name__)

MANAGED_REMARK_PREFIX = "yadreno:"
PANEL_WRITE_LEASE_SECONDS = 120
PANEL_WRITE_TIMEOUT_SECONDS = 90
_HOST_LOCKS: dict[int, asyncio.Lock] = {}


class _CompositionClaimLost(RuntimeError):
    """Internal control flow for a lease/revision fence that no longer holds."""


def managed_subscription_remark(token: str) -> str:
    """Return the stable panel marker for one core-owned binding."""
    normalized = str(token or "").strip()
    if not normalized:
        raise ValueError("managed token is required")
    return f"{MANAGED_REMARK_PREFIX}{normalized}"


def _host_lock(host_key_id: int) -> asyncio.Lock:
    lock = _HOST_LOCKS.get(host_key_id)
    if lock is None:
        lock = asyncio.Lock()
        _HOST_LOCKS[host_key_id] = lock
    return lock


def _row_identity(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("kind") or ""),
        str(row.get("value") or ""),
        str(row.get("remark") or ""),
    )


def _row_snapshot(row: Mapping[str, Any]) -> str:
    """Canonicalize a panel row without discarding foreign fields."""
    return json.dumps(
        dict(row),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _dedupe_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw_row in rows:
        row = dict(raw_row)
        identity = _row_identity(row)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(row)
    return result


def _verify_external_links(
    *,
    actual: Iterable[Mapping[str, Any]],
    foreign: Iterable[Mapping[str, Any]],
    desired: Iterable[Mapping[str, Any]],
    owned_markers: set[str],
) -> bool:
    actual_rows = [dict(row) for row in actual]
    actual_foreign_counts = Counter(
        _row_snapshot(row)
        for row in actual_rows
        if str(row.get("remark") or "") not in owned_markers
    )
    expected_foreign = Counter(_row_snapshot(row) for row in foreign)
    expected_desired = Counter(_row_identity(row) for row in desired)
    for identity, count in expected_foreign.items():
        if actual_foreign_counts[identity] < count:
            return False
    actual_managed = Counter(
        _row_identity(row)
        for row in actual_rows
        if str(row.get("remark") or "") in owned_markers
    )
    if actual_managed != expected_desired:
        return False
    return True


def _retry_delay(attempts: Any) -> int:
    try:
        normalized = max(1, int(attempts or 1))
    except (TypeError, ValueError):
        normalized = 1
    return min(6 * 60 * 60, 30 * (2 ** min(normalized - 1, 9)))


def _host_fingerprint(host: Mapping[str, Any]) -> str:
    """Hash transport identity without persisting panel identifiers."""
    payload = json.dumps(
        [
            int(host.get("server_id") or 0),
            str(host.get("panel_email") or ""),
            str(host.get("sub_id") or ""),
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _safe_error_code(error: BaseException) -> tuple[str, bool]:
    if isinstance(error, TimeoutError):
        return "panel_timeout", False
    if isinstance(error, PanelRequestError):
        if error.kind in {
            PanelErrorKind.UNSUPPORTED_API,
            PanelErrorKind.UNSUPPORTED_VERSION,
        }:
            return "external_links_unsupported", True
        if error.kind is PanelErrorKind.NOT_FOUND:
            return "host_client_missing", False
        if error.kind in {
            PanelErrorKind.UNAUTHORIZED,
            PanelErrorKind.FORBIDDEN,
        }:
            return "panel_auth_unavailable", False
        if error.kind is PanelErrorKind.INVALID_RESPONSE:
            return "panel_invalid_response", False
        if error.kind is PanelErrorKind.TIMEOUT:
            return "panel_timeout", False
        if error.kind is PanelErrorKind.NETWORK:
            return "panel_network", False
        return "panel_request_failed", False
    if isinstance(error, ValueError):
        return "invalid_composition_state", True
    return "panel_sync_failed", False


def _current_status(host_key_id: int) -> dict[str, Any]:
    state = db.get_subscription_composition_sync_state(
        host_key_id=host_key_id,
    )
    if state is None:
        return {
            "ok": False,
            "status": "rejected",
            "error_code": "sync_not_found",
            "host_key_id": host_key_id,
        }
    return {
        "ok": state.get("state") in {"pending", "synced", "retrying", "blocked"},
        "status": str(state.get("state") or "pending"),
        "error_code": state.get("last_error_code"),
        "host_key_id": host_key_id,
        "desired_revision": int(state.get("desired_revision") or 0),
        "applied_revision": int(state.get("applied_revision") or 0),
    }


def _require_live_claim(
    *,
    host_key_id: int,
    lease_owner_token: str,
    desired_revision: int,
) -> None:
    if not db.renew_subscription_composition_sync_lease(
        host_key_id=host_key_id,
        lease_owner_token=lease_owner_token,
        desired_revision=desired_revision,
        lease_seconds=PANEL_WRITE_LEASE_SECONDS,
    ):
        raise _CompositionClaimLost


@regular_panel_operation
async def reconcile_subscription_host(
    *,
    host_key_id: int,
    force: bool = False,
) -> dict[str, Any]:
    """Apply one host's latest desired graph with durable lease/retry state."""
    host_id = int(host_key_id)
    if host_id <= 0:
        return {
            "ok": False,
            "status": "rejected",
            "error_code": "invalid_host_key_id",
        }

    async with _host_lock(host_id):
        if force:
            db.enqueue_subscription_composition_sync(host_key_id=host_id)
        claimed = db.claim_subscription_composition_sync(
            host_key_id=host_id,
            lease_seconds=PANEL_WRITE_LEASE_SECONDS,
        )
        if claimed is None:
            return _current_status(host_id)

        desired_revision = int(claimed.get("desired_revision") or 0)
        lease_owner_token = str(claimed.get("lease_owner_token") or "").strip()
        if not lease_owner_token:
            logger.warning(
                "subscription_composition_claim_missing_owner host_key_id=%s",
                host_id,
            )
            return {
                "ok": False,
                "status": "rejected",
                "error_code": "invalid_sync_claim",
                "host_key_id": host_id,
            }
        applied_tokens = {
            str(token)
            for token in claimed.get("applied_tokens", [])
            if str(token or "").strip()
        }
        host = db.get_vpn_key_by_id(host_id)
        if host is None:
            return {
                "ok": False,
                "status": "rejected",
                "error_code": "host_key_not_found",
                "host_key_id": host_id,
            }

        if not host.get("server_id") or not host.get("panel_email") or not host.get("sub_id"):
            state = db.mark_subscription_composition_sync_blocked(
                host_key_id=host_id,
                error_code="host_not_configured",
                desired_revision=desired_revision,
                lease_owner_token=lease_owner_token,
            )
            status = str((state or {}).get("state") or "blocked")
            return {
                "ok": True,
                "status": status,
                "error_code": "host_not_configured" if status == "blocked" else None,
                "host_key_id": host_id,
                "state": state,
            }
        if int(host.get("server_active") or 0) != 1:
            state = db.mark_subscription_composition_sync_blocked(
                host_key_id=host_id,
                error_code="host_server_inactive",
                desired_revision=desired_revision,
                lease_owner_token=lease_owner_token,
            )
            status = str((state or {}).get("state") or "blocked")
            return {
                "ok": True,
                "status": status,
                "error_code": "host_server_inactive" if status == "blocked" else None,
                "host_key_id": host_id,
                "state": state,
            }

        try:
            client = await vpn_api.get_client(int(host["server_id"]))
            await vpn_api.refresh_client_capabilities(client)
            if not vpn_api.supports_client_external_links(client):
                state = db.mark_subscription_composition_sync_blocked(
                    host_key_id=host_id,
                    error_code="external_links_unsupported",
                    desired_revision=desired_revision,
                    lease_owner_token=lease_owner_token,
                )
                status = str((state or {}).get("state") or "blocked")
                return {
                    "ok": True,
                    "status": status,
                    "error_code": (
                        "external_links_unsupported" if status == "blocked" else None
                    ),
                    "host_key_id": host_id,
                    "state": state,
                }

            current_rows = await vpn_api.get_client_external_links(
                server_id=int(host["server_id"]),
                panel_email=str(host["panel_email"]),
                client=client,
            )
            bindings = db.list_host_subscription_bindings(host_key_id=host_id)
            desired_tokens = {
                str(binding.get("managed_token") or "")
                for binding in bindings
                if str(binding.get("managed_token") or "").strip()
            }
            owned_markers = {
                managed_subscription_remark(token)
                for token in applied_tokens | desired_tokens
            }
            foreign_rows = [
                dict(row)
                for row in current_rows
                if str(row.get("remark") or "") not in owned_markers
            ]

            desired_rows: list[dict[str, Any]] = []
            materialized_tokens: set[str] = set()
            unresolved_error: str | None = None
            unresolved_retry = False
            for binding in bindings:
                _require_live_claim(
                    host_key_id=host_id,
                    lease_owner_token=lease_owner_token,
                    desired_revision=desired_revision,
                )
                component = db.get_vpn_key_by_id(int(binding["component_key_id"]))
                token = str(binding["managed_token"])
                marker = managed_subscription_remark(token)
                if (
                    component is None
                    or not component.get("server_id")
                    or not component.get("sub_id")
                ):
                    unresolved_error = unresolved_error or "component_not_configured"
                    preserved = [
                        dict(row)
                        for row in current_rows
                        if str(row.get("remark") or "") == marker
                    ]
                    desired_rows.extend(preserved)
                    if preserved:
                        materialized_tokens.add(token)
                    continue
                try:
                    component_url = await vpn_api.get_subscription_url_for_key(
                        component,
                        suppress_errors=False,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    _require_live_claim(
                        host_key_id=host_id,
                        lease_owner_token=lease_owner_token,
                        desired_revision=desired_revision,
                    )
                    component_error, component_blocked = _safe_error_code(error)
                    if unresolved_error is None or not component_blocked:
                        unresolved_error = f"component_{component_error}"
                    unresolved_retry = unresolved_retry or not component_blocked
                    preserved = [
                        dict(row)
                        for row in current_rows
                        if str(row.get("remark") or "") == marker
                    ]
                    desired_rows.extend(preserved)
                    if preserved:
                        materialized_tokens.add(token)
                    continue
                _require_live_claim(
                    host_key_id=host_id,
                    lease_owner_token=lease_owner_token,
                    desired_revision=desired_revision,
                )
                if not component_url:
                    unresolved_error = (
                        unresolved_error or "component_subscription_unavailable"
                    )
                    preserved = [
                        dict(row)
                        for row in current_rows
                        if str(row.get("remark") or "") == marker
                    ]
                    desired_rows.extend(preserved)
                    if preserved:
                        materialized_tokens.add(token)
                    continue
                desired_rows.append(
                    {
                        "kind": "subscription",
                        "value": str(component_url),
                        "remark": marker,
                    }
                )
                materialized_tokens.add(token)

            merged_rows = [*foreign_rows, *desired_rows]
            if current_rows != merged_rows:
                _require_live_claim(
                    host_key_id=host_id,
                    lease_owner_token=lease_owner_token,
                    desired_revision=desired_revision,
                )
                await asyncio.wait_for(
                    vpn_api.replace_client_external_links(
                        server_id=int(host["server_id"]),
                        panel_email=str(host["panel_email"]),
                        links=merged_rows,
                        client=client,
                    ),
                    timeout=PANEL_WRITE_TIMEOUT_SECONDS,
                )

            verified_rows = await vpn_api.get_client_external_links(
                server_id=int(host["server_id"]),
                panel_email=str(host["panel_email"]),
                client=client,
            )
            if not _verify_external_links(
                actual=verified_rows,
                foreign=foreign_rows,
                desired=desired_rows,
                owned_markers=owned_markers,
            ):
                raise PanelRequestError(
                    PanelErrorKind.INVALID_RESPONSE,
                    endpoint="/panel/api/clients/:email/externalLinks",
                    detail="external links verification failed",
                )

            if unresolved_error is None:
                state = db.mark_subscription_composition_sync_result(
                    host_key_id=host_id,
                    desired_revision=desired_revision,
                    applied_tokens=sorted(materialized_tokens),
                    host_fingerprint=_host_fingerprint(host),
                    lease_owner_token=lease_owner_token,
                )
                status = str((state or {}).get("state") or "synced")
                error_code = None
            else:
                state = db.mark_subscription_composition_sync_partial(
                    host_key_id=host_id,
                    desired_revision=desired_revision,
                    applied_tokens=sorted(materialized_tokens),
                    error_code=unresolved_error,
                    retry_after_seconds=(
                        _retry_delay(claimed.get("attempts"))
                        if unresolved_retry
                        else None
                    ),
                    host_fingerprint=_host_fingerprint(host),
                    lease_owner_token=lease_owner_token,
                )
                status = str((state or {}).get("state") or "blocked")
                error_code = (
                    unresolved_error if status in {"blocked", "retrying"} else None
                )
            logger.info(
                "subscription_composition_reconciled host_key_id=%s "
                "bindings=%s status=%s",
                host_id,
                len(bindings),
                status,
            )
            return {
                "ok": True,
                "status": status,
                "error_code": error_code,
                "host_key_id": host_id,
                "binding_count": len(bindings),
                "state": state,
            }
        except asyncio.CancelledError:
            raise
        except _CompositionClaimLost:
            db.release_subscription_composition_sync_lease(
                host_key_id=host_id,
                lease_owner_token=lease_owner_token,
            )
            return _current_status(host_id)
        except Exception as error:
            error_code, blocked = _safe_error_code(error)
            if blocked:
                state = db.mark_subscription_composition_sync_blocked(
                    host_key_id=host_id,
                    error_code=error_code,
                    desired_revision=desired_revision,
                    lease_owner_token=lease_owner_token,
                )
                status = str((state or {}).get("state") or "blocked")
            else:
                state = db.mark_subscription_composition_sync_retry(
                    host_key_id=host_id,
                    error_code=error_code,
                    retry_after_seconds=_retry_delay(claimed.get("attempts")),
                    desired_revision=desired_revision,
                    lease_owner_token=lease_owner_token,
                )
                status = str((state or {}).get("state") or "retrying")
            logger.warning(
                "subscription_composition_reconcile_failed host_key_id=%s "
                "status=%s error_code=%s error_type=%s",
                host_id,
                status,
                error_code,
                type(error).__name__,
            )
            return {
                "ok": True,
                "status": status,
                "error_code": error_code if status != "pending" else None,
                "host_key_id": host_id,
                "state": state,
            }


async def process_due_subscription_compositions(
    *,
    limit: int = 50,
) -> dict[str, int]:
    """Process due host rows without letting one panel failure stop the pass."""
    due = db.list_due_subscription_composition_hosts(limit=limit)
    stats = {
        "seen": len(due),
        "synced": 0,
        "pending": 0,
        "retrying": 0,
        "blocked": 0,
        "rejected": 0,
    }
    for row in due:
        result = await reconcile_subscription_host(
            host_key_id=int(row["host_key_id"]),
        )
        status = str(result.get("status") or "rejected")
        if status not in stats:
            status = "rejected"
        stats[status] += 1
    return stats


def enqueue_all_subscription_compositions_for_drift() -> int:
    """Wake every known host so capability recovery and panel drift are found."""
    return int(db.enqueue_subscription_composition_drift())


__all__ = [
    "MANAGED_REMARK_PREFIX",
    "enqueue_all_subscription_compositions_for_drift",
    "managed_subscription_remark",
    "process_due_subscription_compositions",
    "reconcile_subscription_host",
]
