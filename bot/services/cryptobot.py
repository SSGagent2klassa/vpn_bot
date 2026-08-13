"""Thin Crypto Pay API client for the built-in CryptoBot payment adapter."""
from __future__ import annotations

import asyncio
import weakref
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from urllib.parse import urlparse

import aiohttp

from database.requests import get_setting

from bot.services.payment_api import (
    PaymentApiRateLimitError,
    PaymentApiResponseError,
    PaymentApiTransientError,
    payment_client_timeout,
    run_payment_api_operation,
)


CRYPTOBOT_API_URL = "https://pay.crypt.bot/api"
CRYPTOBOT_INVOICE_EXPIRES_SECONDS = 3600
CRYPTOBOT_PROVIDER_ID = "cryptobot"
_LIFECYCLE_LOCKS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    asyncio.Lock,
] = weakref.WeakKeyDictionary()


@dataclass(frozen=True)
class CryptoBotInvoiceCheck:
    """Validated status and safe provider metadata for one exact invoice."""

    status: str
    metadata: Mapping[str, Any]


@dataclass(frozen=True)
class CryptoBotCancellation:
    """Outcome of a fail-closed external invoice cancellation attempt."""

    outcome: str
    metadata: Mapping[str, Any]


def cryptobot_lifecycle_lock() -> asyncio.Lock:
    """Return the current event loop's invoice creation/cancellation lock."""
    loop = asyncio.get_running_loop()
    lock = _LIFECYCLE_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _LIFECYCLE_LOCKS[loop] = lock
    return lock


def _configured_token() -> str:
    return str(get_setting("cryptobot_api_token", "") or "").strip()


def _validated_token(token: str | None = None) -> str:
    value = str(token if token is not None else _configured_token()).strip()
    if not value or any(ord(character) < 32 for character in value):
        raise PaymentApiResponseError("Crypto Pay API token is not configured")
    return value


def _error_code(payload: Mapping[str, Any], *, secret: str = "") -> str:
    value = str(payload.get("error") or "Crypto Pay API rejected the request")
    sanitized = value.replace("\r", " ").replace("\n", " ")
    if secret:
        sanitized = sanitized.replace(secret, "[redacted]")
    return sanitized[:200]


async def _request(
    method: str,
    *,
    params: Mapping[str, Any] | None = None,
    token: str | None = None,
    retry: bool,
    order_id: str | None = None,
) -> Any:
    api_token = _validated_token(token)
    operation = str(method).strip()
    if not operation:
        raise ValueError("Crypto Pay method is required")

    async def call() -> Any:
        headers = {
            "Crypto-Pay-API-Token": api_token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        async with aiohttp.ClientSession(timeout=payment_client_timeout()) as session:
            async with session.post(
                f"{CRYPTOBOT_API_URL}/{operation}",
                headers=headers,
                json=dict(params or {}),
            ) as response:
                try:
                    payload = await response.json(content_type=None)
                except Exception as error:
                    if response.status >= 500:
                        raise PaymentApiTransientError(
                            f"Crypto Pay HTTP {response.status} returned invalid JSON"
                        ) from error
                    raise PaymentApiResponseError(
                        f"Crypto Pay HTTP {response.status} returned invalid JSON"
                    ) from error

                if response.status == 429:
                    raise PaymentApiRateLimitError("Crypto Pay HTTP 429")
                if response.status >= 500:
                    raise PaymentApiTransientError(
                        f"Crypto Pay HTTP {response.status}"
                    )
                if response.status < 200 or response.status >= 300:
                    raise PaymentApiResponseError(
                        f"Crypto Pay HTTP {response.status}"
                    )
                if not isinstance(payload, dict):
                    raise PaymentApiResponseError(
                        "Crypto Pay API returned a non-object response"
                    )
                if payload.get("ok") is not True:
                    raise PaymentApiResponseError(
                        _error_code(payload, secret=api_token)
                    )
                if "result" not in payload:
                    raise PaymentApiResponseError(
                        "Crypto Pay API response has no result"
                    )
                return payload["result"]

    return await run_payment_api_operation(
        provider=CRYPTOBOT_PROVIDER_ID,
        operation=operation,
        order_id=order_id,
        call=call,
        retry=retry,
    )


async def validate_cryptobot_token(token: str) -> Mapping[str, Any]:
    """Validate an administrator-provided token without persisting it."""
    result = await _request("getMe", token=token, retry=True)
    if not isinstance(result, dict):
        raise PaymentApiResponseError("Crypto Pay getMe returned invalid app data")
    return result


async def create_cryptobot_invoice(
    *,
    order_id: str,
    amount: Decimal,
    fiat: str,
    description: str,
) -> dict[str, Any]:
    """Create one fiat-denominated invoice without retrying the mutation."""
    normalized_order_id = str(order_id).strip()
    normalized_fiat = str(fiat).strip().upper()
    normalized_amount = _positive_decimal(amount)
    if not normalized_order_id:
        raise ValueError("order_id is required")
    if normalized_fiat not in {"RUB", "USD"}:
        raise ValueError("Crypto Pay fiat currency must be RUB or USD")

    result = await _request(
        "createInvoice",
        params={
            "currency_type": "fiat",
            "fiat": normalized_fiat,
            "amount": _decimal_text(normalized_amount),
            "description": str(description or "")[:1024],
            "payload": normalized_order_id,
            "expires_in": CRYPTOBOT_INVOICE_EXPIRES_SECONDS,
        },
        retry=False,
        order_id=normalized_order_id,
    )
    invoice = _invoice_mapping(result, operation="createInvoice")
    invoice_id = _invoice_id(invoice)
    payment_url = str(invoice.get("bot_invoice_url") or "").strip()
    parsed_url = urlparse(payment_url)
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        raise PaymentApiResponseError(
            "Crypto Pay createInvoice returned an invalid bot_invoice_url"
        )

    _validate_invoice_identity(
        invoice,
        invoice_id=invoice_id,
        order_id=normalized_order_id,
        amount=normalized_amount,
        fiat=normalized_fiat,
    )
    metadata = _safe_invoice_metadata(invoice)
    metadata["expires_in"] = CRYPTOBOT_INVOICE_EXPIRES_SECONDS
    return {
        "provider_payment_id": invoice_id,
        "payment_url": payment_url,
        "status": _normalized_invoice_status(invoice),
        "metadata": metadata,
    }


async def get_cryptobot_invoice(
    invoice_id: str,
    *,
    order_id: str | None = None,
) -> Mapping[str, Any] | None:
    """Return the exact invoice requested through getInvoices, if present."""
    normalized_id = str(invoice_id).strip()
    if not normalized_id:
        raise ValueError("invoice_id is required")
    result = await _request(
        "getInvoices",
        params={"invoice_ids": normalized_id},
        retry=True,
        order_id=order_id,
    )
    for invoice in _invoice_items(result):
        if str(invoice.get("invoice_id") or "").strip() == normalized_id:
            return invoice
    return None


async def check_cryptobot_invoice(
    *,
    invoice_id: str,
    order_id: str,
    amount: Decimal,
    fiat: str,
) -> CryptoBotInvoiceCheck:
    """Validate the immutable invoice snapshot before mapping its status."""
    invoice = await get_cryptobot_invoice(invoice_id, order_id=order_id)
    if invoice is None:
        raise PaymentApiResponseError("Crypto Pay invoice was not found")
    _validate_invoice_identity(
        invoice,
        invoice_id=str(invoice_id),
        order_id=str(order_id),
        amount=_positive_decimal(amount),
        fiat=str(fiat).upper(),
    )
    return CryptoBotInvoiceCheck(
        status=_normalized_invoice_status(invoice),
        metadata=_safe_invoice_metadata(invoice),
    )


async def delete_cryptobot_invoice(
    invoice_id: str,
    *,
    order_id: str,
) -> None:
    """Delete one active invoice without blindly retrying the mutation."""
    result = await _request(
        "deleteInvoice",
        params={"invoice_id": str(invoice_id)},
        retry=False,
        order_id=order_id,
    )
    if result is not True:
        raise PaymentApiResponseError(
            "Crypto Pay deleteInvoice did not confirm deletion"
        )


async def cancel_cryptobot_invoice(
    *,
    invoice_id: str,
    order_id: str,
    amount: Decimal,
    fiat: str,
) -> CryptoBotCancellation:
    """Delete an unpaid invoice and fail closed when finality is uncertain."""
    try:
        checked = await check_cryptobot_invoice(
            invoice_id=invoice_id,
            order_id=order_id,
            amount=amount,
            fiat=fiat,
        )
    except Exception:
        return CryptoBotCancellation("uncertain", {})

    if checked.status == "succeeded":
        return CryptoBotCancellation("succeeded", checked.metadata)
    if checked.status == "canceled":
        return CryptoBotCancellation("canceled", checked.metadata)

    try:
        await delete_cryptobot_invoice(invoice_id, order_id=order_id)
    except Exception:
        try:
            reconciled = await check_cryptobot_invoice(
                invoice_id=invoice_id,
                order_id=order_id,
                amount=amount,
                fiat=fiat,
            )
        except Exception:
            return CryptoBotCancellation("uncertain", checked.metadata)
        if reconciled.status in {"succeeded", "canceled"}:
            return CryptoBotCancellation(reconciled.status, reconciled.metadata)
        return CryptoBotCancellation("uncertain", reconciled.metadata)

    metadata = dict(checked.metadata)
    metadata["deleted"] = True
    return CryptoBotCancellation("canceled", metadata)


def _invoice_mapping(result: Any, *, operation: str) -> Mapping[str, Any]:
    if not isinstance(result, dict):
        raise PaymentApiResponseError(
            f"Crypto Pay {operation} returned invalid invoice data"
        )
    return result


def _invoice_items(result: Any) -> list[Mapping[str, Any]]:
    if isinstance(result, list):
        items = result
    elif isinstance(result, dict) and isinstance(result.get("items"), list):
        items = result["items"]
    elif isinstance(result, dict) and result.get("invoice_id") is not None:
        items = [result]
    else:
        raise PaymentApiResponseError("Crypto Pay getInvoices returned invalid data")
    return [item for item in items if isinstance(item, dict)]


def _invoice_id(invoice: Mapping[str, Any]) -> str:
    value = str(invoice.get("invoice_id") or "").strip()
    if not value:
        raise PaymentApiResponseError("Crypto Pay invoice_id is missing")
    return value


def _validate_invoice_identity(
    invoice: Mapping[str, Any],
    *,
    invoice_id: str,
    order_id: str,
    amount: Decimal,
    fiat: str,
) -> None:
    if _invoice_id(invoice) != str(invoice_id):
        raise PaymentApiResponseError("Crypto Pay invoice_id mismatch")
    if str(invoice.get("payload") or "") != str(order_id):
        raise PaymentApiResponseError("Crypto Pay invoice payload mismatch")
    if str(invoice.get("currency_type") or "").casefold() != "fiat":
        raise PaymentApiResponseError("Crypto Pay invoice currency_type mismatch")
    if str(invoice.get("fiat") or "").upper() != str(fiat).upper():
        raise PaymentApiResponseError("Crypto Pay invoice fiat mismatch")
    try:
        actual_amount = Decimal(str(invoice.get("amount")))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise PaymentApiResponseError("Crypto Pay invoice amount is invalid") from error
    if not actual_amount.is_finite() or actual_amount != amount:
        raise PaymentApiResponseError("Crypto Pay invoice amount mismatch")


def _normalized_invoice_status(invoice: Mapping[str, Any]) -> str:
    status = str(invoice.get("status") or "").strip().casefold()
    if status == "paid":
        return "succeeded"
    if status == "active":
        return "pending"
    if status == "expired":
        return "canceled"
    raise PaymentApiResponseError("Crypto Pay invoice status is unsupported")


def _safe_invoice_metadata(invoice: Mapping[str, Any]) -> dict[str, Any]:
    allowed = (
        "currency_type",
        "fiat",
        "amount",
        "paid_asset",
        "paid_amount",
        "paid_fiat_rate",
        "paid_usd_rate",
        "fee_asset",
        "fee_amount",
        "created_at",
        "expiration_date",
        "paid_at",
    )
    metadata = {
        key: invoice[key]
        for key in allowed
        if invoice.get(key) is not None
        and isinstance(invoice.get(key), (str, int, float, bool))
    }
    metadata["invoice_id"] = _invoice_id(invoice)
    return metadata


def _positive_decimal(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("Crypto Pay amount must be a decimal") from error
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Crypto Pay amount must be positive")
    return amount


def _decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


__all__ = [
    "CRYPTOBOT_API_URL",
    "CRYPTOBOT_INVOICE_EXPIRES_SECONDS",
    "CryptoBotCancellation",
    "CryptoBotInvoiceCheck",
    "cancel_cryptobot_invoice",
    "check_cryptobot_invoice",
    "create_cryptobot_invoice",
    "cryptobot_lifecycle_lock",
    "delete_cryptobot_invoice",
    "get_cryptobot_invoice",
    "validate_cryptobot_token",
]
