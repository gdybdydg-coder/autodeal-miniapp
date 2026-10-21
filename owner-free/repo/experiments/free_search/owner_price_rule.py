"""Owner rule, exact decimal arithmetic with no display rounding in decisions."""
from decimal import Decimal, InvalidOperation


def evaluate(lower_usd, asking_usd, subscription_percent):
    try:
        lower, asking, percent = map(lambda x: Decimal(str(x)),
                                    (lower_usd, asking_usd, subscription_percent))
    except (InvalidOperation, ValueError):
        raise ValueError('invalid_owner_price_inputs')
    if (not all(x.is_finite() for x in (lower, asking, percent))
            or lower <= 0 or asking <= 0 or not 0 <= percent < 100):
        raise ValueError('invalid_owner_price_inputs')
    reference = lower * Decimal('0.95')
    maximum = reference * (1 - percent / 100)
    return dict(lower_usd=str(lower), reference_usd=str(reference),
                maximum_price_usd=str(maximum), required_discount_percent=str(percent),
                discount_percent=str((reference-asking)/reference*100),
                eligible=asking <= maximum, pricing_method='public_lower_minus5_then_subscription')
