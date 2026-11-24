import pytest

from shipping import shipping_fee


@pytest.mark.parametrize(
    ("subtotal_cents", "expected_fee_cents"),
    [(4_999, 500), (5_000, 0), (5_001, 0)],
)
def test_free_shipping_threshold(subtotal_cents: int, expected_fee_cents: int):
    assert shipping_fee(subtotal_cents) == expected_fee_cents, (
        "Orders of at least 5000 cents qualify for free shipping."
    )
