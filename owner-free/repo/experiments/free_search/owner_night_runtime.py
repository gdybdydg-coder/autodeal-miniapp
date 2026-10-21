"""Local night coordination only. No network, production state or credentials."""
import argparse
import os
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import sys
import time

REPO = Path(__file__).resolve().parents[2]
if __name__ == '__main__':
    sys.path.insert(0, str(REPO))
from experiments.free_search.owner_budget import Budget, Denied

WORKSPACE = Path(os.environ.get('AUTODEAL_OWNER_STATE_DIR', str(REPO.parent))).resolve()
DEADLINE = datetime(2026, 10, 20, 4, 59, tzinfo=timezone.utc).timestamp()
RENEWED_START = datetime(2026, 10, 20, 21, 40, tzinfo=timezone.utc).timestamp()
RENEWED_DEADLINE = datetime(2026, 10, 21, 4, 59, tzinfo=timezone.utc).timestamp()
LOCK = WORKSPACE / 'night-20261020-runtime.sqlite3'
LEDGER = WORKSPACE / 'night-20261020.sqlite3'


class Coordinator:
    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.db.execute('CREATE TABLE IF NOT EXISTS executor '
                        '(id INTEGER PRIMARY KEY CHECK(id=1), token TEXT NOT NULL, started REAL NOT NULL)')

    def close(self):
        self.db.close()

    def acquire(self, token, now, *, renewed=False):
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', token):
            raise ValueError('invalid_token')
        start, deadline = (RENEWED_START, RENEWED_DEADLINE) if renewed else (0, DEADLINE)
        if not start < now < deadline:
            raise ValueError('outside_night')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if self.db.execute('SELECT 1 FROM executor').fetchone():
                raise ValueError('executor_busy')
            self.db.execute('INSERT INTO executor VALUES(1,?,?)', (token, now))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def release(self, token):
        # Token comparison and removal are one atomic operation. Never reclaim by age.
        if self.db.execute('DELETE FROM executor WHERE id=1 AND token=?', (token,)).rowcount != 1:
            raise ValueError('not_lock_owner')

    def status(self):
        row = self.db.execute('SELECT started FROM executor WHERE id=1').fetchone()
        return {'busy': row is not None, 'started': row[0] if row else None}

    def owns(self, token):
        return self.db.execute('SELECT 1 FROM executor WHERE token=?', (token,)).fetchone() is not None


class NightBudget(Budget):
    def reserve(self, url, cap, now, robots_allowed=False):
        if time.time() >= DEADLINE or now >= DEADLINE:
            raise Denied('night_ended')
        return super().reserve(url, cap, now, robots_allowed)


def night_budget():
    if time.time() >= DEADLINE:
        raise ValueError('night_ended')
    return NightBudget(LEDGER, max_gets=40, max_bytes=100000000)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['acquire', 'acquire21', 'release', 'status', 'budget'])
    parser.add_argument('token', nargs='?')
    args = parser.parse_args()
    if args.action == 'budget':
        budget = night_budget()
        try:
            print(json.dumps(budget.summary()))
        finally:
            budget.close()
        return
    coordinator = Coordinator(LOCK)
    try:
        if args.action in ('acquire', 'acquire21'):
            coordinator.acquire(args.token or '', time.time(), renewed=args.action == 'acquire21')
        elif args.action == 'release':
            coordinator.release(args.token or '')
        print(json.dumps(coordinator.status()))
    finally:
        coordinator.close()


if __name__ == '__main__':
    sys.path.insert(0, str(REPO))
    main()
