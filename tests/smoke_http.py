"""End-to-end smoke test over HTTP. Run: python tests/smoke_http.py

Starts the real service on a spare port with a 4-second work cycle, walks
through start -> feedback -> break -> skip, then checks the JSONL record.
It deliberately starts a cycle without a dstask task, so nothing in the real
task list is created, completed or otherwise touched.
"""

import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"


def request(path, payload=None, headers=None):
    data = None
    base_headers = {"X-Intent-Log": "1"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        base_headers["Content-Type"] = "application/json"
    base_headers.update(headers or {})
    req = urllib.request.Request(BASE + path, data=data, headers=base_headers,
                                 method="POST" if payload is not None else "GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=10) as response:
        body = response.read()
        if response.headers.get("Content-Type", "").startswith("application/json"):
            return json.loads(body.decode("utf-8"))
        return body


def state():
    return request("/api/state")


def wait_for(phase, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = state()
        if current["cycle"]["phase"] == phase:
            return current
        time.sleep(0.3)
    raise AssertionError(f"phase {phase} never reached; last={state()}")


def main():
    tmp = tempfile.TemporaryDirectory()
    data_dir = Path(tmp.name)
    server = subprocess.Popen(
        [sys.executable, "app.py", "--port", str(PORT), "--work-seconds", "4",
         "--break-seconds", "3", "--data-dir", str(data_dir), "--no-browser"],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    checks = []
    try:
        for _ in range(50):
            try:
                state()
                break
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.2)
        else:
            raise AssertionError("server did not start")

        # static page + read-only task listing
        html = request("/").decode("utf-8")
        assert "<title>intent-log</title>" in html
        checks.append("GET / serves the page")
        tasks = request("/api/tasks")
        assert isinstance(tasks["tasks"], list)
        checks.append(f"GET /api/tasks lists {len(tasks['tasks'])} open dstask tasks")

        # sync config is read-only here: POST /api/sync would hit the network
        sync = request("/api/sync")
        assert isinstance(sync["config"], dict)
        checks.append(
            "GET /api/sync reports origin: " + str(sync["config"]["remote"])
            + " on branch " + str(sync["config"]["branch"])
        )
        assert "version" in state()
        checks.append("state reports version " + state()["version"])

        log_sync = request("/api/log-sync")
        assert log_sync["config"]["git"] is False, log_sync
        checks.append("GET /api/log-sync reports the plain data directory")

        try:
            request("/api/log-sync", {})
            raise AssertionError("log sync ran on a non-git directory")
        except urllib.error.HTTPError as exc:
            assert exc.code == 502, exc.code
        checks.append("POST /api/log-sync refuses a directory without git")

        # start -> work
        started = request("/api/cycle/start", {})
        assert started["cycle"]["phase"] == "work", started
        assert 2 <= started["cycle"]["remaining_seconds"] <= 4
        checks.append("POST /api/cycle/start -> work")

        # pause freezes the countdown
        paused = request("/api/cycle/pause", {})
        assert paused["cycle"]["phase"] == "work_paused"
        before = paused["cycle"]["remaining_seconds"]
        time.sleep(1.2)
        assert state()["cycle"]["remaining_seconds"] == before
        checks.append("pause freezes the countdown")
        request("/api/cycle/pause", {})

        # run out -> feedback, no automatic completion
        feedback = wait_for("feedback")
        assert feedback["cycle"]["task"] is None
        checks.append("work end -> feedback without touching dstask")

        # reject empty feedback
        try:
            request("/api/cycle/feedback", {"status": "progress", "content": " ",
                                            "next_action": "same_task"})
            raise AssertionError("empty feedback was accepted")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
        checks.append("empty feedback rejected with 400")

        after = request("/api/cycle/feedback", {
            "status": "progress",
            "content": "Smoke test: end-to-end path works",
            "next_action": "same_task",
        })
        assert after["cycle"]["phase"] == "break", after
        checks.append("feedback saved -> break")

        skipped = request("/api/cycle/skip-break", {})
        assert skipped["cycle"]["phase"] == "idle"
        checks.append("skip break -> idle")

        records = request("/api/records")["records"]
        assert len(records) == 1, records
        record = records[0]
        assert record["status"] == "progress"
        assert record["work_seconds"] >= 3, record
        assert record["content"] == "Smoke test: end-to-end path works"
        assert record["schema_version"] == 1
        checks.append(f"JSONL row recorded (work_seconds={record['work_seconds']})")

        lines = (data_dir / "sessions.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        checks.append("one cycle -> exactly one line in sessions.jsonl")

        # state file is restored by a fresh process
        saved = json.loads((data_dir / "state.json").read_text(encoding="utf-8"))
        assert saved["phase"] == "idle"
        checks.append("state.json persisted")

        # CSRF guard
        try:
            request("/api/cycle/start", {}, headers={"X-Intent-Log": "0"})
            raise AssertionError("missing header was accepted")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        checks.append("POST without X-Intent-Log rejected with 403")

        # a bad remote URL must be refused before git is ever called
        try:
            request("/api/sync/remote", {"url": "ftp://example.com/tasks.git"})
            raise AssertionError("an invalid remote URL was accepted")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
        checks.append("invalid sync remote rejected with 400")
    finally:
        server.terminate()
        try:
            output = server.communicate(timeout=10)[0]
        except subprocess.TimeoutExpired:
            server.kill()
            output = server.communicate()[0]
        tmp.cleanup()

    for line in checks:
        print("  OK  " + line)
    print(f"\n{len(checks)} smoke checks passed")
    if output.strip():
        print("\n--- server log ---")
        print(output.strip()[-2000:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
