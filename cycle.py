"""The 20-minute work cycle state machine.

    idle -> work -> (paused) -> feedback -> break -> idle
                    \\-> feedback (ended early)

One work cycle at a time. Every second of work is counted from a monotonic
clock minus paused time, so a paused cycle cannot inflate the recorded
duration. The state is mirrored to a small JSON file on every transition, so
refreshing or reopening the page (or restarting the service) never loses a
pending feedback.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from store import SCHEMA_VERSION

LOGGER = logging.getLogger("intent_log.cycle")

STATUSES = ("progress", "task_done", "blocked", "interrupted", "off_task")
NEXT_ACTIONS = ("same_task", "choose_task", "finish")


class CycleError(RuntimeError):
    """A request the current state cannot satisfy (HTTP 409)."""


class ValidationError(ValueError):
    """Bad input from the caller (HTTP 400)."""


def _now():
    return datetime.now().astimezone()


def _iso(moment):
    return moment.isoformat(timespec="seconds")


class CycleManager:
    def __init__(
        self,
        store,
        tasks,
        state_path,
        work_seconds=20 * 60,
        break_seconds=5 * 60,
        clock=time.monotonic,
    ):
        self._store = store
        self._tasks = tasks
        self._state_path = Path(state_path)
        self.work_seconds = int(work_seconds)
        self.break_seconds = int(break_seconds)
        self._clock = clock
        self._lock = threading.RLock()
        self._phase = "idle"           # idle | work | feedback | break
        self._paused = False
        self._task = None
        self._planned_seconds = self.work_seconds
        self._started_at = None
        self._ended_at = None
        self._elapsed_offset = 0.0     # counted work seconds
        self._start_mono = None
        self._break_offset = 0.0
        self._break_start_mono = None
        self._break_reason = None
        self._last_task = None
        self._warnings = []
        self._on_feedback_needed = None
        self._on_work_finished = None
        self._load()

    # ------------------------------------------------------------------ state

    def set_callbacks(self, on_feedback_needed=None, on_work_finished=None):
        """on_feedback_needed() fires when the work segment reaches its end;
        on_work_finished(record) fires after a completed segment is stored."""
        self._on_feedback_needed = on_feedback_needed
        self._on_work_finished = on_work_finished

    def _work_elapsed(self):
        if self._phase not in ("work", "feedback"):
            return float(self._planned_seconds)
        elapsed = self._elapsed_offset
        if self._start_mono is not None:
            elapsed += max(0.0, self._clock() - self._start_mono)
        return elapsed

    def work_seconds_done(self):
        if self._phase in ("work", "feedback"):
            return int(round(self._work_elapsed()))
        return 0

    def _break_elapsed(self):
        elapsed = self._break_offset
        if self._break_start_mono is not None:
            elapsed += max(0.0, self._clock() - self._break_start_mono)
        return elapsed

    def remaining_seconds(self):
        if self._phase == "work":
            return max(0, int(round(self.work_seconds - self._work_elapsed())))
        if self._phase == "break":
            return max(0, int(round(self.break_seconds - self._break_elapsed())))
        return 0

    def snapshot(self):
        with self._lock:
            phase = self._phase
            if self._paused and phase in ("work", "break"):
                phase = f"{phase}_paused"
            return {
                "phase": phase,
                "running": self._phase in ("work", "break") and not self._paused,
                "task": dict(self._task) if self._task else None,
                "last_task": dict(self._last_task) if self._last_task else None,
                "planned_seconds": self._planned_seconds,
                "work_seconds": self.work_seconds_done(),
                "remaining_seconds": self.remaining_seconds(),
                "started_at": self._started_at,
                "ended_at": self._ended_at,
                "break_seconds": self.break_seconds,
                "break_reason": self._break_reason,
                "warnings": list(self._warnings),
                "settings": {
                    "work_seconds": self.work_seconds,
                    "break_seconds": self.break_seconds,
                },
            }

    # -------------------------------------------------------------- transitions

    def start(self, task_uuid=None):
        with self._lock:
            if self._phase != "idle":
                raise CycleError("已经有一个周期在进行中，先结束或取消它")
            task = None
            if task_uuid:
                task = self._tasks.find_open(task_uuid)
                if task is None:
                    raise ValidationError("找不到这个待办任务，可能已完成或已被删除")
            self._task = (
                {
                    "uuid": task["uuid"],
                    "id": task.get("id", ""),
                    "title": task["summary"],
                    "project": task.get("project", ""),
                }
                if task
                else None
            )
            self._phase = "work"
            self._paused = False
            self._planned_seconds = self.work_seconds
            self._started_at = _iso(_now())
            self._ended_at = None
            self._elapsed_offset = 0.0
            self._start_mono = self._clock()
            self._break_offset = 0.0
            self._break_start_mono = None
            self._break_reason = None
            self._warnings = []
            self._save()
            LOGGER.info("Cycle started (%s)", self._task["title"] if self._task else "no task")
            return self.snapshot()

    def pause(self):
        with self._lock:
            if self._phase not in ("work", "break"):
                raise CycleError("当前没有可以暂停的计时")
            if not self._paused:
                if self._phase == "work":
                    self._elapsed_offset = self._work_elapsed()
                    self._start_mono = None
                else:
                    self._break_offset = self._break_elapsed()
                    self._break_start_mono = None
                self._paused = True
            else:
                now = self._clock()
                if self._phase == "work":
                    self._start_mono = now
                else:
                    self._break_start_mono = now
                self._paused = False
            self._save()
            return self.snapshot()

    def cancel(self):
        """Discard the current segment without recording it (work phase only)."""
        with self._lock:
            if self._phase == "feedback":
                raise CycleError("已经到反馈这一步了，请提交或先取消记录")
            if self._phase not in ("work", "break"):
                raise CycleError("当前没有进行中的周期")
            if self._phase == "work":
                LOGGER.info("Cycle cancelled without a record")
            self._reset_to_idle(keep_last=True)
            self._save()
            return self.snapshot()

    def end_cycle(self):
        """End the work segment early and wait for feedback.

        The recorded `work_seconds` is the real elapsed time, not the planned
        1200 seconds.
        """
        with self._lock:
            if self._phase != "work":
                raise CycleError("当前没有正在进行的工作周期")
            self._enter_feedback()
            return self.snapshot()

    def tick(self):
        """Advance timers. Safe to call from any thread, any time."""
        notify = False
        with self._lock:
            if self._phase == "work" and not self._paused:
                if self.remaining_seconds() <= 0:
                    self._enter_feedback()
                    notify = True
            elif self._phase == "break" and not self._paused:
                if self.remaining_seconds() <= 0:
                    self._break_offset = self._break_elapsed()
                    self._break_start_mono = None
                    self._reset_to_idle(keep_last=True)
                    self._save()
        if notify and self._on_feedback_needed:
            try:
                self._on_feedback_needed()
            except Exception as exc:  # never break the timer for a popup
                LOGGER.warning("Could not open the feedback page: %s", exc)
        return self.snapshot()

    def _enter_feedback(self):
        self._elapsed_offset = self._work_elapsed()
        self._start_mono = None
        self._ended_at = _iso(_now())
        self._phase = "feedback"
        self._save()
        LOGGER.info("Work segment finished, waiting for feedback")

    def _reset_to_idle(self, keep_last=True):
        if keep_last and self._task:
            self._last_task = dict(self._task)
        self._phase = "idle"
        self._paused = False
        self._task = None
        self._started_at = None
        self._ended_at = None
        self._planned_seconds = self.work_seconds
        self._elapsed_offset = 0.0
        self._start_mono = None
        self._break_offset = 0.0
        self._break_start_mono = None
        self._break_reason = None

    # ---------------------------------------------------------------- feedback

    def submit_feedback(self, status, content, next_action):
        with self._lock:
            if self._phase != "feedback":
                raise CycleError("现在没有等待反馈的工作段")
            status = self._validate_status(status)
            next_action = self._validate_next_action(next_action)
            content = self._validate_content(content)

            record = {
                "schema_version": SCHEMA_VERSION,
                "session_id": self._session_id(),
                "task_id": self._task["uuid"] if self._task else None,
                "task_title": self._task["title"] if self._task else None,
                "task_project": self._task["project"] if self._task else None,
                "started_at": self._started_at,
                "ended_at": self._ended_at,
                "planned_seconds": self._planned_seconds,
                "work_seconds": self.work_seconds_done(),
                "feedback_submitted_at": _iso(_now()),
                "status": status,
                "content": content,
                "next_action": next_action,
            }
            # Write first; if the disk fails the feedback stays on screen.
            self._store.append(record)

            warnings = []
            if status == "task_done" and self._task:
                try:
                    self._tasks.complete(self._task["id"], content)
                except Exception as exc:
                    warnings.append(f"记录已保存，但 dstask 标记完成失败：{exc}")
            self._warnings = warnings

            finished_task = dict(self._task) if self._task else None
            if next_action == "finish":
                self._reset_to_idle(keep_last=False)
                self._last_task = finished_task
                self._save()
            else:
                self._reset_to_idle(keep_last=True)
                self._last_task = finished_task
                self._begin_break(next_action)
            LOGGER.info(
                "Feedback stored: status=%s work=%ss next=%s",
                status, record["work_seconds"], next_action,
            )
            snapshot = self.snapshot()
        if self._on_work_finished:
            try:
                self._on_work_finished(record)
            except Exception as exc:
                LOGGER.warning("Post-feedback hook failed: %s", exc)
        return snapshot

    def _begin_break(self, next_action):
        self._phase = "break"
        self._paused = False
        self._break_offset = 0.0
        self._break_start_mono = self._clock()
        self._break_reason = next_action
        self._save()

    def skip_break(self):
        with self._lock:
            if self._phase != "break":
                raise CycleError("当前不在休息中")
            self._reset_to_idle(keep_last=True)
            self._save()
            return self.snapshot()

    @staticmethod
    def _validate_status(status):
        if status not in STATUSES:
            raise ValidationError("status 必须是 " + " / ".join(STATUSES))
        return status

    @staticmethod
    def _validate_next_action(next_action):
        if next_action not in NEXT_ACTIONS:
            raise ValidationError("next_action 必须是 " + " / ".join(NEXT_ACTIONS))
        return next_action

    @staticmethod
    def _validate_content(content):
        if not isinstance(content, str) or not content.strip():
            raise ValidationError("反馈内容不能为空")
        content = content.strip()
        if len(content) > 500:
            raise ValidationError("反馈内容请控制在 500 字以内")
        return content

    def _session_id(self):
        stamp = (self._started_at or _iso(_now())).replace(":", "").replace("-", "")
        return f"{stamp}-{os.getpid()}-{int(time.time() * 1000) % 1000:03d}"

    # ------------------------------------------------------------ persistence

    def _save(self):
        state = {
            "schema_version": SCHEMA_VERSION,
            "phase": self._phase,
            "paused": self._paused,
            "task": self._task,
            "last_task": self._last_task,
            "planned_seconds": self._planned_seconds,
            "started_at": self._started_at,
            "ended_at": self._ended_at,
            "elapsed_offset": self._elapsed_offset,
            "break_offset": self._break_offset,
            "break_seconds": self.break_seconds,
            "break_reason": self._break_reason,
            "saved_at": _iso(_now()),
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self._state_path)
        except OSError as exc:
            LOGGER.error("Could not persist cycle state: %s", exc)

    def _load(self):
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        phase = state.get("phase")
        if phase not in ("work", "feedback", "break"):
            return
        self._phase = phase
        self._paused = bool(state.get("paused")) and phase in ("work", "break")
        self._task = state.get("task") if isinstance(state.get("task"), dict) else None
        last = state.get("last_task")
        self._last_task = last if isinstance(last, dict) else None
        self._planned_seconds = int(state.get("planned_seconds") or self.work_seconds)
        self._started_at = state.get("started_at")
        self._ended_at = state.get("ended_at")
        self._break_reason = state.get("break_reason")
        if "break_seconds" in state:
            self.break_seconds = int(state["break_seconds"])
        if phase == "work":
            # Time while the service was down is never counted as work.
            self._elapsed_offset = float(state.get("elapsed_offset") or 0.0)
            if not self._paused:
                self._start_mono = self._clock()
        elif phase == "break":
            self._break_offset = float(state.get("break_offset") or 0.0)
            if not self._paused:
                self._break_start_mono = self._clock()
        LOGGER.info("Restored %s phase from %s", phase, self._state_path.name)


class TimerThread(threading.Thread):
    """Calls CycleManager.tick() a few times per second."""

    def __init__(self, cycle, interval=0.5):
        super().__init__(daemon=True, name="cycle-timer")
        self._cycle = cycle
        self._interval = interval
        self._stop_event = threading.Event()

    def run(self):
        while not self._stop_event.wait(self._interval):
            try:
                self._cycle.tick()
            except Exception as exc:
                LOGGER.exception("Timer tick failed: %s", exc)

    def stop(self):
        self._stop_event.set()
