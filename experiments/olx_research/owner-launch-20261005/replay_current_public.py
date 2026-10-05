"""Offline replay of the immutable six-card packet; never fetches sources.

Pass the scratch source directory containing the original HTML and receipts.
Raw VIN, contacts and descriptions are deliberately excluded from output.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sys


def denied(*args, **kwargs):
    raise RuntimeError('owner_launch_replay_network_forbidden')


# Install before any project import. No app, .env or production transport import.
for name in ('connect', 'connect_ex', 'sendto', 'sendmsg'):
    if hasattr(socket.socket, name):
        setattr(socket.socket, name, denied)
socket.create_connection = denied
socket.getaddrinfo = denied
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.olx_market import ria_public_reference as public
from backend.olx_market.evaluation import evaluate_holdout


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source_directory', type=Path)
    args = parser.parse_args()
    output = Path(__file__).resolve().parent
    source = args.source_directory
    now = int(datetime.now(timezone.utc).timestamp())
    packet = json.loads((source / 'dense-details-receipt.json').read_text())
    split = json.loads((output / 'dense-split-freeze.json').read_text())
    dictionary = public.generation_dictionary_from_public_html(
        (source / 'ria-catalog.html').read_bytes(),
        json.loads((source / 'ria-catalog-receipt.json').read_text()), now=now)
    cars, rows = [], []
    for receipt in packet['requests']:
        sid = receipt['id']
        provenance = {'role': receipt['role'],
            'frozen_commit': packet['freeze_commit'], 'frozen_at': packet['frozen_at'],
            'search_params': {'category_id': 1, 'marka_id[0]': 70, 'model_id[0]': 652}}
        checked = int(datetime.fromisoformat(receipt['completed_at']).timestamp())
        try:
            car = public.from_public_html((source / ('dense-' + sid + '.html')).read_bytes(),
                sid, checked, provenance, receipt, now=now, generation_dictionary=dictionary)
        except ValueError as exc:
            rows.append({'id': sid, 'role': receipt['role'], 'adapter_error': str(exc)})
            continue
        cars.append(car)
        rows.append({'source': car['source'], 'id': sid, 'role': receipt['role'],
            'source_admission_reasons': public.reasons(car, now),
            'asking_price': car['price'], 'currency': car['currency'],
            **{key: car.get(key) for key in ('year', 'mileage_km', 'body', 'fuel',
                'engine_cc', 'drive_type', 'power_hp', 'generation_variant')},
            'condition': car['research_condition'],
            'vin_key_corroborated': car['vehicle_identity_verified'],
            'photos_reviewed': car['identity_review']['distinct_photos_reviewed']})
    evaluation = evaluate_holdout(cars, split['membership'], None, now, minimum=8,
        split_provenance=[{'seed': 'owner-public-dense-price-blind-v1',
            'basis': 'explicit_frozen_price_blind_before_full_card_prices',
            'commit': packet['freeze_commit'], 'frozen_at': packet['frozen_at']}])
    result = {'kind': 'six_current_frozen_full_public_html_not_a_ready_market_profile',
        'checked_at': now, 'attempted': len(packet['requests']), 'parsed': len(cars),
        'source_admission_complete': sum(not public.reasons(c, now) for c in cars),
        'source_complete_reference': sum(c['reference_provenance']['role'] == 'reference'
            and not public.reasons(c, now) for c in cars),
        'source_complete_holdout': sum(c['reference_provenance']['role'] == 'holdout'
            and not public.reasons(c, now) for c in cars),
        'minimum': 8, 'independent_photo_reviews': 0, 'owner_authorized': True,
        'client_authorized': False, 'technical_ready': False, 'telegram_attempts': 0,
        'method_selected': None, 'frozen_total': len(split['membership']),
        'captured_reference': sum(c['reference_provenance']['role'] == 'reference' for c in cars),
        'captured_holdout': sum(c['reference_provenance']['role'] == 'holdout' for c in cars),
        'frozen_controls': evaluation['frozen_holdout_count'],
        'estimated_controls': evaluation['estimated_holdout_count'],
        'max_compatible_control_references': max(
            (r['review']['assessment']['sample'] for r in evaluation['rows']), default=0),
        'evaluation': evaluation, 'rows': rows}
    for filename, value in (('dense-current-details-sanitized.json', cars),
                            ('dense-current-evaluation.json', result)):
        (output / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k != 'evaluation'},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
