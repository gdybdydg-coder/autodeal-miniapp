"""Run from repo root with: python3 -I experiments/free_search/test_owner_offline.py"""
import os
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root))
# No application/.env is loaded. Guard installed BEFORE backend/test imports.
from experiments.free_search.network_guard import OutboundGuard

guard = OutboundGuard()
with guard.isolated():
    import unittest
    suite = unittest.defaultTestLoader.discover(str(root / 'experiments/free_search/tests'),
                                              pattern='test_owner_pilot.py')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
print('OFFLINE', guard.summary())
raise SystemExit(0 if result.wasSuccessful() and guard.successful_external_calls == 0 else 1)
