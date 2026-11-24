def shipping_fee(subtotal_cents: int) -> int:
    """Calculate the shipping fee in cents."""
    return 0 if subtotal_cents > 5_000 else 500
