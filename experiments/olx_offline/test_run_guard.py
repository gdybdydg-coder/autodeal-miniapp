import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments.olx_offline.run_guard import GuardStopped, HARD_STOP, RunGuard, main


class Clock:
    def __init__(self, wall=HARD_STOP - 3600):
        self.wall, self.mono = wall, 100.0

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


class RunGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clock = Clock()

    def guard(self, **kwargs):
        return RunGuard(self.root, clock=lambda: self.clock.wall,
                        monotonic=lambda: self.clock.mono, **kwargs)

    def test_context_required_and_instance_cannot_restart_budget(self):
        guard = self.guard()
        with self.assertRaises(GuardStopped):
            guard.reserve_request("www.olx.ua", 1024)
        with guard:
            self.assertEqual(guard.remaining_seconds(), 1800)
        with self.assertRaises(GuardStopped):
            guard.checkpoint()
        with self.assertRaises(GuardStopped):
            with guard:
                pass

    def test_local_lock_excludes_overlap_and_releases_after_error(self):
        with self.assertRaisesRegex(RuntimeError, "fixture failure"):
            with self.guard():
                with self.assertRaisesRegex(GuardStopped, "holds the lock"):
                    with self.guard():
                        self.fail("overlapping run was allowed")
                raise RuntimeError("fixture failure")
        with self.guard() as later:
            later.checkpoint()
        self.assertTrue((self.root / "olx-research.lock").exists())

    def test_stop_file_before_run_and_between_operations(self):
        stop = self.root / "STOP"
        stop.touch()
        with self.assertRaisesRegex(GuardStopped, "STOP"):
            with self.guard():
                pass
        stop.unlink()
        with self.guard() as guard:
            stop.touch()
            with self.assertRaisesRegex(GuardStopped, "STOP"):
                guard.reserve_request("www.olx.ua", 1024)
            self.assertEqual(guard.counters["olx"]["requests"], 0)

    def test_broken_stop_symlink_also_stops(self):
        (self.root / "STOP").symlink_to(self.root / "absent")
        with self.assertRaises(GuardStopped):
            with self.guard():
                pass

    def test_maximum_duration_and_wall_clock_rollback(self):
        with self.guard(max_seconds=10) as guard:
            self.clock.advance(9)
            self.assertEqual(guard.remaining_seconds(), 1)
            self.clock.wall -= 600
            self.clock.mono += 1
            with self.assertRaisesRegex(GuardStopped, "deadline"):
                guard.checkpoint()

    def test_kyiv_hard_cutoff_shortens_run_and_prevents_later_runs(self):
        self.clock.wall = HARD_STOP - 15
        with self.guard() as guard:
            self.assertEqual(guard.remaining_seconds(), 15)
            self.clock.advance(15)
            with self.assertRaises(GuardStopped):
                guard.checkpoint()
        with self.assertRaises(GuardStopped):
            with self.guard():
                pass

    def test_no_refunds_and_request_caps_are_independent(self):
        with self.guard() as guard:
            for _ in range(4):
                reservation = guard.reserve_request("www.olx.ua", 2 * 1024 * 1024)
                guard.record_response(reservation, 0)  # Failed attempt still charged.
            nbu = guard.reserve_request("bank.gov.ua", 256 * 1024)
            guard.record_response(nbu, 100)
            self.assertEqual(guard.counters["olx"]["reserved_bytes"], 8 * 1024 * 1024)
            self.assertEqual(guard.counters["nbu"]["received_bytes"], 100)
            with self.assertRaisesRegex(GuardStopped, "budget"):
                guard.reserve_request("olx.ua", 1)
            self.assertEqual(guard.counters["olx"]["requests"], 4)

    def test_byte_caps_and_nbu_request_cap(self):
        with self.guard() as guard:
            with self.assertRaises(GuardStopped):
                guard.reserve_request("olx.ua", 8 * 1024 * 1024 + 1)
            self.assertEqual(guard.counters["olx"]["requests"], 0)
        with self.guard() as guard:
            with self.assertRaises(GuardStopped):
                guard.reserve_request("bank.gov.ua", 256 * 1024 + 1)
        with self.guard() as guard:
            guard.reserve_request("bank.gov.ua", 1)
            with self.assertRaises(GuardStopped):
                guard.reserve_request("bank.gov.ua", 1)

    def test_invalid_host_or_cap_is_not_charged(self):
        with self.guard() as guard:
            for host in ("evil.olx.ua", "api.auto.ria.com", "https://www.olx.ua/", "api.telegram.org"):
                with self.assertRaises(ValueError):
                    guard.reserve_request(host, 1)
            for cap in (0, -1, 1.5, True):
                with self.assertRaises(ValueError):
                    guard.reserve_request("olx.ua", cap)
            self.assertEqual(guard.counters["olx"]["requests"], 0)

    def test_response_count_once_and_oversized_reader_fails_closed(self):
        with self.guard() as guard:
            reservation = guard.reserve_request("olx.ua", 50)
            guard.record_response(reservation, 30)
            with self.assertRaises(ValueError):
                guard.record_response(reservation, 30)
            next_reservation = guard.reserve_request("olx.ua", 50)
            with self.assertRaises(GuardStopped):
                guard.record_response(next_reservation, 51)
            with self.assertRaises(GuardStopped):
                guard.reserve_request("bank.gov.ua", 1)

    def test_atomic_state_saved_before_request_and_at_exit(self):
        with self.guard(persist_state=True) as guard:
            reservation = guard.reserve_request("olx.ua", 1024)
            state = json.loads((self.root / "run-state.json").read_text())
            self.assertEqual(state["counters"]["olx"]["requests"], 1)
            self.assertEqual(state["counters"]["olx"]["reserved_bytes"], 1024)
            guard.record_response(reservation, 20)
        state = json.loads((self.root / "run-state.json").read_text())
        self.assertEqual(state["phase"], "finished")
        self.assertEqual(state["counters"]["olx"]["received_bytes"], 20)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_cli_inspection_does_not_create_files_or_issue_network(self):
        with patch("socket.socket", side_effect=AssertionError("Network forbidden")):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                result = main(["inspect", "--work-root", str(self.root)])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["lock_status"], "not_checked")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_invalid_duration_or_root(self):
        for seconds in (0, -1, 1801, float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                self.guard(max_seconds=seconds)
        for root in ("relative", "/", str(self.root / "absent")):
            with self.assertRaises(ValueError):
                RunGuard(root)


if __name__ == "__main__":
    unittest.main()
