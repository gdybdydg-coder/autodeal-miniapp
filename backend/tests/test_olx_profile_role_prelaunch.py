"""Offline frozen-split regressions; synthetic cohorts are not live readiness."""
from copy import deepcopy
import hashlib
import json

import pytest

from backend import olx_owner_feed as feed
from backend.olx_market import ria_reference as ria
from backend.tests.test_olx_ria_reference import info, profile, provenance
from backend.tests.test_olx_owner_feed import live, synthetic_car
from backend.tests.test_olx_isolated_integration import bench


@pytest.mark.parametrize('wrong', ['role', 'commit', 'frozen_at'])
def test_reference_receipt_must_match_actual_frozen_split(live, wrong):
    p = profile(live)
    assert feed.profile_ready(p, live.clock[0])
    old = p['reference'][0]
    receipt = provenance(live.clock[0])
    if wrong == 'role':
        receipt['role'] = 'holdout'
    elif wrong == 'commit':
        receipt['frozen_commit'] = 'c' * 40
    else:
        receipt['frozen_at'] -= 1
    replacement = ria.from_full_info(info(old['id'], int(old['price'])), old['id'],
                                     int(live.clock[0]), receipt)
    replacement['vehicle_key'] = old['vehicle_key']
    replacement['identity_review'] = deepcopy(old['identity_review'])
    # Rebuild through the adapter so receipt integrity remains valid.
    p['reference'][0] = replacement
    assert ria.reasons(replacement) == []
    assert not feed.profile_ready(p, live.clock[0])


def test_explicit_source_id_split_keeps_distinct_platform_ids(live):
    p = profile(live)
    other = synthetic_car(live, p['reference'][0]['id'], '8075',
                          generation_variant='FL')
    p['reference'].append(other)
    p['split_key_format'] = 'source_id_v1'
    membership = {c['source'] + ':' + c['id']: role
                  for role in ('reference', 'holdout') for c in p[role]}
    p['split_freeze']['membership_sha256'] = hashlib.sha256(
        json.dumps(membership, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    assert feed.profile_ready(p, live.clock[0])
    # An old id-only receipt cannot silently be reinterpreted as this split.
    p.pop('split_key_format')
    assert not feed.profile_ready(p, live.clock[0])
