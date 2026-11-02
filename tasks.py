"""Thin wrapper around the `dstask` CLI.

Only three operations touch dstask in v1: listing open tasks, adding a task and
completing one. Starting a 20-minute cycle never changes dstask state, so a
stray timer cannot silently modify the task list.
"""

import json
import re
import subprocess

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
DSTASK_TIMEOUT = 30


class DstaskError(RuntimeError):
    pass


def _run(*args):
    try:
        return subprocess.run(
            ["dstask", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=DSTASK_TIMEOUT,
        )
    except FileNotFoundError:
        raise DstaskError("dstask was not found in PATH")
    except subprocess.TimeoutExpired:
        raise DstaskError("dstask timed out")


def _clean(result):
    return ANSI_RE.sub("", result.stdout + result.stderr).strip()


class DstaskTasks:
    def _list(self, command):
        result = _run(command, "--")
        if result.returncode != 0:
            raise DstaskError(_clean(result) or "Could not read tasks")
        try:
            return json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            raise DstaskError("Could not parse the dstask output")

    @staticmethod
    def _public(task):
        status = task.get("status")
        if status == "resolved":
            mapped = "completed"
        elif status == "active":
            mapped = "active"
        else:
            mapped = "pending"
        due = task.get("due") or ""
        return {
            "id": str(task.get("id", "")),
            "uuid": task["uuid"],
            "status": mapped,
            "summary": task["summary"],
            "project": task.get("project", "") or "",
            "due": None if due.startswith("0001-") else due,
            "completed": task.get("resolved") if mapped == "completed" else None,
        }

    def list_open(self):
        return [self._public(task) for task in self._list("show-open")]

    def list_completed(self, limit=100):
        tasks = [self._public(task) for task in self._list("show-resolved")]
        tasks.sort(key=lambda task: task.get("completed") or "", reverse=True)
        return tasks[:limit]

    def find_open(self, task_uuid):
        for task in self.list_open():
            if task["uuid"] == task_uuid:
                return task
        return None

    def add(self, description, project=""):
        result = _run("add", description, "--")
        output = _clean(result)
        if result.returncode != 0:
            raise DstaskError(output or "Adding the task failed")
        match = re.search(r"Added (\d+):", output)
        if not match:
            raise DstaskError("Could not determine the new task id")
        task_id = match.group(1)
        if project:
            modify = _run("modify", task_id, f"project:{project}", "--")
            if modify.returncode != 0:
                raise DstaskError(_clean(modify) or "Could not set the project")
        for task in self._list("show-open"):
            if str(task.get("id")) == task_id:
                return self._public(task)
        raise DstaskError("The new task could not be found after adding it")

    def complete(self, task_id, feedback=""):
        """Mark a dstask task done. `task_id` is the short numeric id."""
        args = ["done", str(task_id)]
        if feedback:
            args.append(feedback)
        args.append("--")
        result = _run(*args)
        if result.returncode != 0:
            raise DstaskError(_clean(result) or "Could not mark the task as done")

    def sync(self):
        result = _run("sync")
        output = _clean(result)
        if result.returncode != 0:
            raise DstaskError(output or "Sync failed")
        return output
