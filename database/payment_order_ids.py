"""Internal identifiers shared by payment intents and non-payment history rows."""

BASE62_ALPHABET = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'


def encode_base62(value: int) -> str:
    """Encode one non-negative integer as a compact Base62 string."""
    number = int(value)
    if number < 0:
        raise ValueError('value must be non-negative')
    if number == 0:
        return BASE62_ALPHABET[0]

    result: list[str] = []
    while number > 0:
        number, remainder = divmod(number, 62)
        result.append(BASE62_ALPHABET[remainder])
    return ''.join(reversed(result))


def build_payment_order_id(payment_id: int) -> str:
    """Build the stable internal order id associated with a payments row."""
    return f'00{encode_base62(payment_id)}'


__all__ = ['build_payment_order_id', 'encode_base62']
