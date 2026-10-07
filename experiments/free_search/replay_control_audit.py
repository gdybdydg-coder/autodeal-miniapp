"""Offline replay of a saved control audit. No fetching or Telegram sending.

python -m experiments.free_search.replay_control_audit RESULT.json MANIFEST.json
"""
import json
from pathlib import Path
from .control_audit import audit_controls


def replay(result, manifest):
    # Observation timestamps come from saved timings of the collector pages.
    discoveries = result['discovery_observations']
    reproduced = audit_controls([c['id'] for c in manifest['candidates']],
                                discoveries, result['details'], result['valuations'])
    if reproduced != result['audit']:
        raise ValueError('control_audit_replay_mismatch')
    return reproduced


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('result', type=Path)
    parser.add_argument('manifest', type=Path)
    args = parser.parse_args()
    outcome = replay(json.loads(args.result.read_text()), json.loads(args.manifest.read_text()))
    print(json.dumps({k: v for k, v in outcome.items() if k != 'rows'}, ensure_ascii=False))
