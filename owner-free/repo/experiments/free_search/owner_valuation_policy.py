"""Historical asking-price research policy; NOT a current pilot release gate."""
from .valuation import ValuationPolicy

OWNER_VALUATION_POLICY = ValuationPolicy(
    min_peers=5, max_peer_age_seconds=86400, max_subject_price_age_seconds=300)
