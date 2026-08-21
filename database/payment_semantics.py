"""Shared SQL semantics for classifying successful key payments."""
from __future__ import annotations

import re


_SQL_ALIAS_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def paid_key_purchase_predicate(alias: str) -> str:
    """Return the canonical SQL predicate for a paid key purchase or renewal."""
    if not isinstance(alias, str) or not _SQL_ALIAS_RE.fullmatch(alias):
        raise ValueError('SQL alias must be a simple identifier')
    return f"""
        {alias}.status = 'paid'
        AND COALESCE(
            NULLIF({alias}.purpose, ''),
            'historical_key_payment'
        ) IN (
            'historical_key_payment',
            'key_purchase',
            'key_renewal'
        )
        AND COALESCE({alias}.payment_type, '') NOT IN (
            'trial',
            'promo_free',
            'demo'
        )
        AND COALESCE({alias}.is_promo_free, 0) = 0
    """


__all__ = ['paid_key_purchase_predicate']
