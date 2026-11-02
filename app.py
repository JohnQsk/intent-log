"""One local service for 20-minute work cycles with a feedback log.

    python app.py                 # http://127.0.0.1:8770
    python app.py --work-seconds 60 --break-seconds 10   # short test cycle

The same HTTP API serves the browser page and any agent (Cow), so both always
see one state. Bound to 127.0.0.1 only.
"""

import argparse
import json
import logging
import threading
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from cycle import CycleError, CycleManager, TimerThread, ValidationError
from store import SessionStore
from syncer import DstaskRepo, SyncError
from tasks import DstaskError, DstaskTasks

VERSION = "0.1.1"

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
MAX_BODY = 64 * 1024
DEFAULT_DATA_DIR = Path.home() / ".intent-log"


class IntentLogServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, address, handler, cycle, tasks, store, repo, public_url=""):
        super().__init__(address, handler)
        self.cycle = cycle
        self.tasks = tasks
        self.store = store
        self.repo = repo
        self.public_url = public_url
        self.last_sync = None


class Handler(BaseHTTPRequestHandler):
    server_version = f"IntentLog/{VERSION}"

    def log_message(self, _format, *_args):
        pass

    # ------------------------------------------------------------ plumbing

    def _send_bytes(self, status, content, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def _send_json(self, payload, status=200):
        self._send_bytes(
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _error(self, message, status=400):
        self._send_json({"error": message}, status)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValidationError("Invalid Content-Length")
        if length < 0 or length > MAX_BODY:
            raise ValidationError("Request body is too large")
        if length == 0:
            return {}
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            raise ValidationError("A JSON body is required")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValidationError("Could not parse the JSON body")
        if not isinstance(payload, dict):
            raise ValidationError("The JSON body must be an object")
        return payload

    def _state_payload(self):
        cycle_state = self.server.cycle.snapshot()
        today = date.today().isoformat()
        return {
            "version": VERSION,
            "cycle": cycle_state,
            "today": self.server.store.summary_for(today),
        }

    def _sync_payload(self):
        return {
            "config": self.server.repo.status(),
            "last": self.server.last_sync,
        }

    # ---------------------------------------------------------------- routes

    def do_GET(self):
        parts = urlsplit(self.path)
        path = parts.path
        try:
            if path == "/api/state":
                self._send_json(self._state_payload())
            elif path == "/api/tasks":
                self._send_json({"tasks": self.server.tasks.list_open()})
            elif path == "/api/tasks/completed":
                self._send_json({"tasks": self.server.tasks.list_completed()})
            elif path == "/api/sync":
                self._send_json(self._sync_payload())
            elif path == "/api/records":
                query = parse_qs(parts.query)
                day = (query.get("date") or [date.today().isoformat()])[0]
                store = self.server.store
                self._send_json(
                    {
                        "date": day,
                        "records": store.records(day),
                        "days": store.days(),
                        "summary": store.summary_for(day),
                    }
                )
            elif path in ("/", "/index.html", "/app.js", "/style.css"):
                files = {
                    "/": ("index.html", "text/html; charset=utf-8"),
                    "/index.html": ("index.html", "text/html; charset=utf-8"),
                    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                    "/style.css": ("style.css", "text/css; charset=utf-8"),
                }
                name, content_type = files[path]
                self._send_bytes(200, (STATIC_DIR / name).read_bytes(), content_type)
            elif path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
            else:
                self._error("Not found", 404)
        except (CycleError, ValidationError, SyncError) as exc:
            status = 409 if isinstance(exc, CycleError) else 400
            self._error(str(exc), status)
        except DstaskError as exc:
            self._error(str(exc), 502)
        except OSError as exc:
            self._error(f"Read failed: {exc}", 500)

    def do_POST(self):
        if self.headers.get("X-Intent-Log") != "1":
            self._error("Forbidden", 403)
            return
        path = urlsplit(self.path).path
        cycle = self.server.cycle
        try:
            payload = self._read_json()
            if path == "/api/cycle/start":
                state = cycle.start(payload.get("task_id") or None)
            elif path == "/api/cycle/pause":
                state = cycle.pause()
            elif path == "/api/cycle/end":
                state = cycle.end_cycle()
            elif path == "/api/cycle/cancel":
                state = cycle.cancel()
            elif path == "/api/cycle/skip-break":
                state = cycle.skip_break()
            elif path == "/api/cycle/feedback":
                state = cycle.submit_feedback(
                    payload.get("status"),
                    payload.get("content"),
                    payload.get("next_action"),
                )
                self._send_json(
                    {
                        "cycle": state,
                        "today": self.server.store.summary_for(date.today().isoformat()),
                    }
                )
                return
            elif path == "/api/tasks":
                description = payload.get("description")
                project = payload.get("project") or ""
                if not isinstance(description, str) or not description.strip():
                    raise ValidationError("The task description cannot be empty")
                if len(description.strip()) > 1000:
                    raise ValidationError("The task description is too long")
                if not isinstance(project, str) or len(project) > 100:
                    raise ValidationError("Invalid project name")
                task = self.server.tasks.add(description.strip(), project.strip())
                self._send_json({"task": task}, 201)
                return
            elif path == "/api/sync":
                output = self.server.repo.sync()
                self.server.last_sync = {
                    "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "ok": True,
                    "message": output or "Already up to date",
                }
                self._send_json({"output": output, **self._sync_payload()})
                return
            elif path == "/api/sync/remote":
                url = payload.get("url")
                status = self.server.repo.set_remote(url)
                self._send_json({"config": status, "last": self.server.last_sync})
                return
            else:
                self._error("Not found", 404)
                return
            self._send_json(
                {
                    "cycle": state,
                    "today": self.server.store.summary_for(date.today().isoformat()),
                }
            )
        except ValidationError as exc:
            self._error(str(exc), 400)
        except CycleError as exc:
            self._error(str(exc), 409)
        except SyncError as exc:
            message = str(exc)
            if path == "/api/sync":
                self.server.last_sync = {
                    "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "ok": False,
                    "message": message,
                }
            self._error(message, 400 if path == "/api/sync/remote" else 502)
        except DstaskError as exc:
            self._error(str(exc), 502)
        except OSError as exc:
            self._error(
                f"Write failed; your feedback is still on the page and can be retried: {exc}",
                500,
            )


def main():
    parser = argparse.ArgumentParser(
        description="20-minute work cycles with a self-feedback log, one local service."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--work-seconds", type=int, default=20 * 60)
    parser.add_argument("--break-seconds", type=int, default=5 * 60)
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if args.work_seconds < 1 or args.break_seconds < 0:
        parser.error("--work-seconds must be >= 1 and --break-seconds >= 0")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("intent_log")

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    store = SessionStore(data_dir / "sessions.jsonl")
    tasks = DstaskTasks()
    repo = DstaskRepo()
    cycle = CycleManager(
        store=store,
        tasks=tasks,
        state_path=data_dir / "state.json",
        work_seconds=args.work_seconds,
        break_seconds=args.break_seconds,
    )

    try:
        server = IntentLogServer(
            (args.host, args.port), Handler, cycle, tasks, store, repo
        )
    except OSError as exc:
        logger.error("Cannot bind %s:%s (%s)", args.host, args.port, exc)
        return 1

    url = f"http://{args.host}:{server.server_port}"
    server.public_url = url

    def open_feedback_tab():
        logger.info("Work segment finished; opening the feedback page")
        webbrowser.open(f"{url}/?feedback=1", new=2)

    cycle.set_callbacks(on_feedback_needed=open_feedback_tab)

    http_thread = threading.Thread(target=server.serve_forever, daemon=True)
    http_thread.start()
    timer = TimerThread(cycle)
    timer.start()

    logger.info("intent-log %s is running: %s", VERSION, url)
    logger.info("Data: %s", data_dir)
    if not args.no_browser:
        webbrowser.open(url)

    try:
        while True:
            threading.Event().wait(1)
    except KeyboardInterrupt:
        logger.info("Stopped by user")
    finally:
        timer.stop()
        server.shutdown()
        server.server_close()
        http_thread.join(timeout=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
