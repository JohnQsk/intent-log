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
        raise DstaskError("找不到 dstask 命令，确认它已在 PATH 中")
    except subprocess.TimeoutExpired:
        raise DstaskError("dstask 命令超时")


def _clean(result):
    return ANSI_RE.sub("", result.stdout + result.stderr).strip()


class DstaskTasks:
    def _list(self, command):
        result = _run(command, "--")
        if result.returncode != 0:
            raise DstaskError(_clean(result) or "读取任务失败")
        try:
            return json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            raise DstaskError("无法解析 dstask 输出")

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
            raise DstaskError(output or "添加任务失败")
        match = re.search(r"Added (\d+):", output)
        if not match:
            raise DstaskError("无法确定新任务的编号")
        task_id = match.group(1)
        if project:
            modify = _run("modify", task_id, f"project:{project}", "--")
            if modify.returncode != 0:
                raise DstaskError(_clean(modify) or "设置 project 失败")
        for task in self._list("show-open"):
            if str(task.get("id")) == task_id:
                return self._public(task)
        raise DstaskError("添加后找不到新任务")

    def complete(self, task_id, feedback=""):
        """Mark a dstask task done. `task_id` is the short numeric id."""
        args = ["done", str(task_id)]
        if feedback:
            args.append(feedback)
        args.append("--")
        result = _run(*args)
        if result.returncode != 0:
            raise DstaskError(_clean(result) or "标记任务完成失败")

    def sync(self):
        result = _run("sync")
        output = _clean(result)
        if result.returncode != 0:
            raise DstaskError(output or "同步失败")
        return output
