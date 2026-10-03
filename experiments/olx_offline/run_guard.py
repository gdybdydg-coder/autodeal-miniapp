"""Cooperative limits for one isolated research run; no network or background work.

Every caller must checkpoint between operations, reserve BEFORE each request,
bound the reader to that reservation, and cap its timeout by remaining_seconds().
Reservations are not refunded on errors. This does not interrupt an in-flight
operation. flock protects cooperating processes on the SAME machine/filesystem;
it is not a cross-machine lease. Budgets are per invocation, not a nightly total.
Use an explicitly selected scratch directory, never the production work directory.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import tempfile
import time
from zoneinfo import ZoneInfo


HARD_STOP_KYIV = "2026-10-03T08:00:00+03:00"
HARD_STOP = datetime.fromisoformat(HARD_STOP_KYIV).timestamp()
LIMITS = {"olx": (4, 8 * 1024 * 1024), "nbu": (1, 256 * 1024)}
HOSTS = {"olx.ua": "olx", "www.olx.ua": "olx", "bank.gov.ua": "nbu"}


class GuardStopped(RuntimeError):
    """No further research operation is authorized by this local guard."""


@dataclass(frozen=True)
class Reservation:
    number: int
    source: str
    byte_cap: int


class RunGuard:
    def __init__(self, work_root, *, max_seconds=1800, persist_state=False,
                 clock=time.time, monotonic=time.monotonic, hard_stop=HARD_STOP):
        root = Path(work_root)
        if not root.is_absolute() or not root.is_dir() or root.resolve() == Path("/"):
            raise ValueError("work_root must be an existing absolute scratch directory")
        if isinstance(max_seconds, bool) or not math.isfinite(max_seconds) or not 0 < max_seconds <= 1800:
            raise ValueError("max_seconds must be greater than zero and at most 1800")
        self.root = root.resolve()
        if isinstance(hard_stop, bool) or not isinstance(hard_stop, (int, float)) or not math.isfinite(hard_stop):
            raise ValueError('Explicit finite absolute deadline required')
        self.hard_stop = hard_stop
        self.max_seconds = max_seconds
        self.persist_state = persist_state
        self.clock, self.monotonic = clock, monotonic
        self.stop_file = self.root / "STOP"
        self.state_file = self.root / "run-state.json"
        self.lock_file = self.root / "olx-research.lock"
        self._fd = None
        self._entered = False
        self._started = self._mono_start = self._deadline = None
        self._reason = None
        self._phase = "not_started"
        self._reservations = {}
        self._recorded = set()
        self.counters = {source: {"requests": 0, "reserved_bytes": 0, "received_bytes": 0}
                         for source in LIMITS}

    def __enter__(self):
        if self._entered:
            raise GuardStopped("guard instances cannot be reused")
        self._entered = True
        fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise GuardStopped("another local research run holds the lock") from None
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        self._started, self._mono_start = self.clock(), self.monotonic()
        self._deadline = min(self.hard_stop, self._started + self.max_seconds)
        self._phase = "running"
        try:
            self.checkpoint()
            self._save()
        except BaseException:
            self._release()
            raise
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self._reason is None:
            self._phase = "failed" if exc_type else "finished"
        try:
            self._save()
        finally:
            self._release()
        return False

    def _release(self):
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
        # Never unlink the lock: replacement would let another inode be locked.

    def _stop(self, reason):
        self._reason, self._phase = reason, "stopped"
        self._save()
        raise GuardStopped(reason)

    def checkpoint(self):
        if self._fd is None:
            raise GuardStopped("guard must hold its lock inside a with block")
        if self._reason:
            raise GuardStopped(self._reason)
        if self.stop_file.exists() or self.stop_file.is_symlink():
            self._stop("STOP file is present")
        if self.clock() >= self._deadline or self.monotonic() - self._mono_start >= self.max_seconds:
            self._stop("run deadline reached")

    def remaining_seconds(self):
        self.checkpoint()
        return max(0.0, min(self._deadline - self.clock(),
                            self.max_seconds - (self.monotonic() - self._mono_start)))

    def reserve_request(self, host, bytescap):
        """Charge one attempt and its maximum body size; redirects need a new slot.

        Accept a hostname only. This is not URL/path validation and does not grant
        access to arbitrary endpoints on an allowed host. The caller must use
        separately reviewed fixed HTTPS URLs, without automatic retries/redirects.
        """
        self.checkpoint()
        source = HOSTS.get(host)
        if source is None:
            raise ValueError("host is not in the research allowlist")
        if type(bytescap) is not int or bytescap <= 0:
            raise ValueError("bytescap must be a positive integer")
        count_cap, byte_cap = LIMITS[source]
        counter = self.counters[source]
        if counter["requests"] >= count_cap or counter["reserved_bytes"] + bytescap > byte_cap:
            self._stop(source + " request or byte budget exhausted")
        reservation = Reservation(len(self._reservations) + 1, source, bytescap)
        self._reservations[reservation.number] = reservation
        counter["requests"] += 1
        counter["reserved_bytes"] += bytescap
        self._save()  # Persist reservation before caller can attempt the request.
        return reservation

    def record_response(self, reservation, bytes_received):
        """Record consumed body bytes, including partial/error bodies, once only."""
        if self._fd is None:
            raise GuardStopped("guard must hold its lock")
        if self._reservations.get(reservation.number) is not reservation or reservation.number in self._recorded:
            raise ValueError("unknown or already recorded reservation")
        if type(bytes_received) is not int or not 0 <= bytes_received <= reservation.byte_cap:
            self._stop("response exceeded reservation or invalid byte count")
        self.counters[reservation.source]["received_bytes"] += bytes_received
        self._recorded.add(reservation.number)
        self._save()

    def status(self):
        return {"phase": self._phase, "reason": self._reason,
                "started_at_utc": _iso(self._started), "deadline_utc": _iso(self._deadline),
                "hard_stop_kyiv": datetime.fromtimestamp(self.hard_stop, ZoneInfo('Europe/Kyiv')).isoformat(), "max_seconds": self.max_seconds,
                "stop_requested": self.stop_file.exists() or self.stop_file.is_symlink(),
                "counters": {source: dict(values) for source, values in self.counters.items()},
                "limits": {source: {"requests": cap[0], "response_bytes": cap[1]}
                           for source, cap in LIMITS.items()},
                "scope": "one invocation; cooperative; local process lock only"}

    def _save(self):
        if not self.persist_state:
            return
        fd, name = tempfile.mkstemp(prefix="run-state-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.status(), handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.state_file)
            directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(name):
                os.unlink(name)


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat() if timestamp is not None else None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "inspect"))
    parser.add_argument("--work-root", required=True)
    parser.add_argument("--max-seconds", type=float, default=1800)
    parser.add_argument("--persist-state", action="store_true")
    args = parser.parse_args(argv)
    guard = RunGuard(args.work_root, max_seconds=args.max_seconds, persist_state=args.persist_state)
    if args.action == "inspect":
        result = {"stop_requested": guard.status()["stop_requested"],
                  "hard_stop_kyiv": HARD_STOP_KYIV, "hard_stop_passed": time.time() >= HARD_STOP,
                  "lock_status": "not_checked", "saved_state": None}
        if guard.state_file.exists():
            if guard.state_file.stat().st_size > 16384:
                raise ValueError("saved state exceeds 16 KiB")
            result["saved_state"] = json.loads(guard.state_file.read_text(encoding="utf-8"))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    try:
        with guard:
            guard.checkpoint()
        print(json.dumps(guard.status(), ensure_ascii=False, indent=2))
        return 0
    except GuardStopped as exc:
        print(json.dumps({"allowed": False, "reason": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
