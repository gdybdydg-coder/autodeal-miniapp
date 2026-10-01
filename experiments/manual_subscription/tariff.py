"""Owner-approved quote for the isolated test; never enables payment collection."""
# Owner chose 250 UAH for 30 days on 2026-10-01 at 19:36 Europe/Kyiv.
AMOUNT_UAH = 250
DAYS = 30
CURRENCY = 'UAH'
PAYMENTS_ENABLED = False


def confirmed_snapshot(amount, days):
    """Historic fixture quotes remain unchanged and are not newly approved."""
    return type(amount) is int and type(days) is int and (amount, days) == (AMOUNT_UAH, DAYS)
