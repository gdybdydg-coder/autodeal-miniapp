"""Compare source-asserted vehicle identifiers without returning VIN or hashes.

Public page assertions are not independent physical inspections. Unknown or
masked identifiers never establish agreement or absence of duplicates.
"""
import json
import re
from .public_details import parse_public_details, _Scripts, _pairs


def _private_identifier(html, listing_id):
    parse_public_details(html, listing_id)  # Bind every Vehicle to the requested ID.
    parser = _Scripts()
    parser.feed(html)
    queue = [json.loads(block, object_pairs_hook=_pairs) for block in parser.blocks]
    values = []
    while queue:
        node = queue.pop()
        if isinstance(node, list):
            queue.extend(node)
        elif isinstance(node, dict):
            types = node.get('@type', [])
            if types == 'Vehicle' or (isinstance(types, list) and 'Vehicle' in types):
                if 'vehicleIdentificationNumber' in node:
                    value = node['vehicleIdentificationNumber']
                    value = value.upper() if isinstance(value, str) and value.isascii() else None
                    values.append(value if isinstance(value, str) and re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}', value) else None)
            if '@graph' in node:
                queue.append(node['@graph'])
    return values[0] if values and values[0] is not None and all(v == values[0] for v in values) else None


def audit_public_identity(target_pages, peer_pages):
    """Inputs are (listing ID, private HTML). Output contains only listing IDs/counts."""
    targets, peers = list(target_pages), list(peer_pages)
    if not 1 <= len(targets) <= 200 or not 1 <= len(peers) <= 200:
        raise ValueError('identity_page_budget')
    ids = [i for i, _ in targets + peers]
    if len(set(ids)) != len(ids):
        raise ValueError('duplicate_or_overlapping_listing_ids')
    # Raise parser errors unchanged: existing codes never contain source values.
    ts = {i: _private_identifier(h, i) for i, h in targets}
    ps = {i: _private_identifier(h, i) for i, h in peers}
    known_targets = {v for v in ts.values() if v is not None}
    duplicates = sorted(i for i, v in ps.items() if v is not None and v in known_targets)
    groups = {}
    for i, value in ps.items():
        if value is not None:
            groups.setdefault(value, []).append(i)
    repeated = sorted([sorted(ids) for ids in groups.values() if len(ids) > 1])
    return {'planned_targets': len(ts), 'planned_peers': len(ps),
            'target_identifier_known': sum(v is not None for v in ts.values()),
            'peer_identifier_known': sum(v is not None for v in ps.values()),
            'unknown_target_ids': sorted(i for i, v in ts.items() if v is None),
            'unknown_peer_ids': sorted(i for i, v in ps.items() if v is None),
            'source_asserted_cross_target_duplicate_peer_ids': duplicates,
            'source_asserted_peer_duplicate_groups': repeated,
            'physical_identity_proven': False, 'raw_identifiers_retained': False,
            'source_assertions_only': True, 'production_approved': False}
