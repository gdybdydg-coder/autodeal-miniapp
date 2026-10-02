"""Run offline regression suites, with an opt-in Work Mode wakeup workaround.

Normal CI: python tools/offline_pytest.py -q backend/tests
Work sandbox: python tools/offline_pytest.py --sandbox-wakeup -q backend/tests

Backend conftest keeps the network fence; this runner never enables network I/O.
The opt-in only bounds epoll sleeps where thread self-pipe wakeups are unavailable.
It changes no production dispatcher, fixture sender, synchronization or test clock.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
args = sys.argv[1:]
if "--sandbox-wakeup" in args:
    args.remove("--sandbox-wakeup")
    import selectors
    original_select = selectors.EpollSelector.select
    def bounded_select(self, timeout=None):
        return original_select(self, 0.05 if timeout is None else min(timeout, 0.05))
    selectors.EpollSelector.select = bounded_select

import pytest
raise SystemExit(pytest.main(args))
