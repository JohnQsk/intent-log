"""Tests for the cycle state machine. Run: python -m unittest discover -s tests"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cycle import CycleError, CycleManager, ValidationError  # noqa: E402
from store import SessionStore  # noqa: E402


class FakeClock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FakeTasks:
    def __init__(self, tasks=None):
        self.tasks = tasks or [
            {"id": "7", "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
             "summary": "implement LRU cache", "project": "algorithms", "status": "pending"},
            {"id": "8", "uuid": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
             "summary": "review linked lists", "project": "algorithms", "status": "pending"},
        ]
        self.completed = []

    def find_open(self, task_uuid):
        for task in self.tasks:
            if task["uuid"] == task_uuid:
                return task
        return None

    def complete(self, task_id, feedback=""):
        self.completed.append((task_id, feedback))


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = SessionStore(self.root / "sessions.jsonl")
        self.tasks = FakeTasks()
        self.clock = FakeClock()
        self.cycle = self.make_cycle()

    def tearDown(self):
        self.tmp.cleanup()

    def make_cycle(self, work=20 * 60, rest=5 * 60):
        return CycleManager(
            store=self.store,
            tasks=self.tasks,
            state_path=self.root / "state.json",
            work_seconds=work,
            break_seconds=rest,
            clock=self.clock,
        )

    # ---------------------------------------------------------------- helpers

    def start_math(self):
        return self.cycle.start("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    def finish_work(self, work=20 * 60):
        self.start_math()
        self.clock.advance(work)
        snapshot = self.cycle.tick()
        self.assertEqual(snapshot["phase"], "feedback")
        return snapshot

    # ------------------------------------------------------------------ tests

    def test_start_counts_down_and_reaches_feedback(self):
        snapshot = self.start_math()
        self.assertEqual(snapshot["phase"], "work")
        self.assertEqual(snapshot["task"]["title"], "implement LRU cache")
        self.assertEqual(snapshot["remaining_seconds"], 20 * 60)

        self.clock.advance(300)
        self.assertEqual(self.cycle.snapshot()["remaining_seconds"], 15 * 60)

        self.clock.advance(15 * 60)
        snapshot = self.cycle.tick()
        self.assertEqual(snapshot["phase"], "feedback")
        self.assertEqual(snapshot["work_seconds"], 20 * 60)
        # Nothing is written into dstask automatically.
        self.assertEqual(self.tasks.completed, [])

    def test_pause_time_is_not_counted(self):
        self.start_math()
        self.clock.advance(120)
        paused = self.cycle.pause()
        self.assertEqual(paused["phase"], "work_paused")
        self.assertEqual(paused["remaining_seconds"], 20 * 60 - 120)

        self.clock.advance(600)  # paused for ten minutes
        self.assertEqual(self.cycle.snapshot()["remaining_seconds"], 20 * 60 - 120)

        resumed = self.cycle.pause()
        self.assertEqual(resumed["phase"], "work")
        self.clock.advance(60)
        self.cycle.end_cycle()
        snapshot = self.cycle.snapshot()
        self.assertEqual(snapshot["work_seconds"], 180)

    def test_end_early_keeps_real_duration(self):
        self.start_math()
        self.clock.advance(7 * 60 + 5)
        self.cycle.end_cycle()
        snapshot = self.cycle.snapshot()
        self.assertEqual(snapshot["phase"], "feedback")
        self.assertEqual(snapshot["work_seconds"], 425)
        self.assertEqual(snapshot["remaining_seconds"], 0)

    def test_feedback_writes_one_row_and_starts_break(self):
        self.finish_work()
        snapshot = self.cycle.submit_feedback("progress", "Figured out the edge case in question 2", "same_task")
        self.assertEqual(snapshot["phase"], "break")
        self.assertEqual(snapshot["break_reason"], "same_task")

        records = self.store.records()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["schema_version"], 1)
        self.assertEqual(record["task_title"], "implement LRU cache")
        self.assertEqual(record["status"], "progress")
        self.assertEqual(record["content"], "Figured out the edge case in question 2")
        self.assertEqual(record["next_action"], "same_task")
        self.assertEqual(record["work_seconds"], 20 * 60)
        self.assertTrue(record["session_id"])
        self.assertEqual(self.tasks.completed, [])

    def test_task_done_completes_dstask_once(self):
        self.finish_work()
        snapshot = self.cycle.submit_feedback("task_done", "All done", "choose_task")
        self.assertEqual(self.tasks.completed, [("7", "All done")])
        self.assertEqual(snapshot["warnings"], [])

    def test_choose_task_and_finish_differ(self):
        self.finish_work()
        snapshot = self.cycle.submit_feedback("progress", "Made progress", "choose_task")
        self.assertEqual(snapshot["phase"], "break")
        self.assertEqual(snapshot["last_task"]["title"], "implement LRU cache")

        skipped = self.cycle.skip_break()
        self.assertEqual(skipped["phase"], "idle")
        self.assertIsNone(skipped["task"])

        self.finish_work()
        snapshot = self.cycle.submit_feedback("progress", "Stopping for today", "finish")
        self.assertEqual(snapshot["phase"], "idle")
        self.assertEqual(len(self.store.records()), 2)

    def test_break_ends_by_itself(self):
        self.finish_work()
        self.cycle.submit_feedback("progress", "ok", "same_task")
        self.clock.advance(5 * 60)
        snapshot = self.cycle.tick()
        self.assertEqual(snapshot["phase"], "idle")

    def test_same_task_twice_makes_two_rows(self):
        for _ in range(2):
            self.finish_work()
            self.cycle.submit_feedback("progress", "Another round", "same_task")
            self.cycle.skip_break()
        self.assertEqual(len(self.store.records()), 2)

    def test_state_survives_restart(self):
        self.finish_work()
        restarted = self.make_cycle(work=3, rest=2)
        snapshot = restarted.snapshot()
        self.assertEqual(snapshot["phase"], "feedback")
        self.assertEqual(snapshot["task"]["uuid"], "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        # Submitting after a restart still writes the record.
        restarted.submit_feedback("blocked", "Recorded after a restart", "choose_task")
        self.assertEqual(len(self.store.records()), 1)

    def test_running_work_survives_restart_without_offline_time(self):
        self.start_math()
        self.clock.advance(30)
        restarted = self.make_cycle()
        # Restored state saved no elapsed offset yet, so nothing was counted.
        self.assertEqual(restarted.snapshot()["phase"], "work")
        self.clock.advance(10)
        self.assertEqual(restarted.snapshot()["work_seconds"], 10)

    def test_validation(self):
        with self.assertRaises(ValidationError):
            self.cycle.start("ffffffff-ffff-ffff-ffff-ffffffffffff")
        self.cycle.start()
        with self.assertRaises(CycleError):
            self.cycle.start()
        self.cycle.end_cycle()
        with self.assertRaises(ValidationError):
            self.cycle.submit_feedback("done", "x", "same_task")
        with self.assertRaises(ValidationError):
            self.cycle.submit_feedback("progress", "   ", "same_task")
        with self.assertRaises(ValidationError):
            self.cycle.submit_feedback("progress", "x", "later")
        with self.assertRaises(CycleError):
            self.cycle.pause()

    def test_cancel_writes_nothing(self):
        self.start_math()
        self.clock.advance(120)
        snapshot = self.cycle.cancel()
        self.assertEqual(snapshot["phase"], "idle")
        self.assertEqual(self.store.records(), [])

    def test_failed_store_keeps_feedback_pending(self):
        class BrokenStore(SessionStore):
            def append(self, record):
                raise OSError("disk full")

        broken = CycleManager(
            store=BrokenStore(self.root / "broken.jsonl"),
            tasks=self.tasks,
            state_path=self.root / "state2.json",
            work_seconds=60,
            break_seconds=60,
            clock=self.clock,
        )
        broken.start("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        self.clock.advance(60)
        broken.tick()
        with self.assertRaises(OSError):
            broken.submit_feedback("progress", "Cannot write", "choose_task")
        self.assertEqual(broken.snapshot()["phase"], "feedback")

    def test_jsonl_file_shape(self):
        self.finish_work()
        self.cycle.submit_feedback("progress", "Made progress", "finish")
        line = (self.root / "sessions.jsonl").read_text(encoding="utf-8").strip()
        record = json.loads(line)
        for key in (
            "schema_version", "session_id", "task_id", "task_title", "started_at",
            "ended_at", "planned_seconds", "work_seconds", "feedback_submitted_at",
            "status", "content", "next_action",
        ):
            self.assertIn(key, record)


if __name__ == "__main__":
    unittest.main()
