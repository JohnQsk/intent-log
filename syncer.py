"""Git-backed sync helpers.

Two independent repositories are involved:

* `~/.dstask` — the task data. `dstask sync` is `git pull` then `git push`, so
  the sync target is simply that repository's `origin`.
* the intent-log data directory — the append-only session log. Here we commit
  new rows ourselves, merge the remote side, then push.

They are deliberately separate: a conflict in the log must never be able to
block task sync, and dstask's own `git add -A` commits must not sweep log rows
into the task history.
"""

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

DSTASK_DIR = Path.home() / ".dstask"
DEFAULT_LOG_DIR = Path.home() / ".intent-log"

# Only plain git URLs. Anything with whitespace, newlines or a leading dash is
# rejected so a URL can never turn into an option or a second command.
URL_PATTERN = re.compile(
    r"^(?:https?://[^\s]+|ssh://[^\s]+|git@[^\s:]+:[^\s]+|git://[^\s]+)$"
)


class SyncError(RuntimeError):
    pass


def _run(args, timeout=120):
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        raise SyncError(f"{args[0]} was not found in PATH")
    except subprocess.TimeoutExpired:
        raise SyncError(f"{args[0]} timed out after {timeout}s")


def validate_url(url):
    if not isinstance(url, str):
        raise SyncError("The remote URL must be a string")
    url = url.strip()
    if not url or len(url) > 300:
        raise SyncError("The remote URL is empty or too long")
    if not URL_PATTERN.match(url):
        raise SyncError(
            "Use a git URL such as https://host/user/repo.git or "
            "git@host:user/repo.git"
        )
    return url


def _first_line(text):
    lines = [line for line in (text or "").strip().splitlines() if line.strip()]
    return lines[0] if lines else "git failed"


class GitRepo:
    """Read and configure one local git repository."""

    def __init__(self, path):
        self.path = Path(path)

    def _git(self, *args, timeout=60):
        return _run(["git", "-C", str(self.path), *args], timeout=timeout)

    def _is_repo(self):
        if not self.path.is_dir():
            return False
        result = self._git("rev-parse", "--is-inside-work-tree")
        return result.returncode == 0 and result.stdout.strip() == "true"

    def status(self):
        if not self._is_repo():
            return {
                "git": False,
                "path": str(self.path),
                "remote": None,
                "branch": None,
                "dirty": False,
                "ahead": None,
                "behind": None,
            }

        remote = self._git("config", "--get", "remote.origin.url")
        branch = self._git("rev-parse", "--abbrev-ref", "HEAD")
        dirty = self._git("status", "--porcelain")
        ahead = behind = None
        branch_name = branch.stdout.strip() if branch.returncode == 0 else ""
        if branch_name:
            counts = self._git(
                "rev-list", "--left-right", "--count",
                f"origin/{branch_name}...HEAD",
            )
            if counts.returncode == 0:
                parts = counts.stdout.split()
                if len(parts) == 2:
                    behind, ahead = int(parts[0]), int(parts[1])
        return {
            "git": True,
            "path": str(self.path),
            "remote": remote.stdout.strip() if remote.returncode == 0 else None,
            "branch": branch_name or None,
            "dirty": bool(dirty.stdout.strip()) if dirty.returncode == 0 else False,
            "ahead": ahead,
            "behind": behind,
        }

    def has_upstream(self):
        return self._git("rev-parse", "--abbrev-ref", "@{upstream}").returncode == 0

    def set_remote(self, url):
        url = validate_url(url)
        current = self._git("config", "--get", "remote.origin.url")
        if current.returncode == 0 and current.stdout.strip():
            result = self._git("remote", "set-url", "origin", url)
        else:
            result = self._git("remote", "add", "origin", url)
        if result.returncode != 0:
            raise SyncError(_first_line(result.stderr or result.stdout))
        return self.status()


class DstaskRepo(GitRepo):
    """The task repository. Sync is delegated to dstask itself."""

    def __init__(self, path=None):
        super().__init__(path or DSTASK_DIR)

    def sync(self, timeout=180):
        result = _run(["dstask", "sync"], timeout=timeout)
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0:
            raise SyncError(output or "dstask sync failed")
        return output


class LogRepo(GitRepo):
    """The session log repository: commit local rows, merge, push.

    A merge rather than a rebase is used on purpose. With
    `sessions.jsonl merge=union` in .gitattributes both sides' appended rows
    survive a merge; a rebase does not invoke the union driver and can drop the
    other device's rows without reporting a conflict. As a second guard, the
    set of session ids seen before and after the pull is compared, and nothing
    is pushed if any row disappeared.
    """

    def __init__(self, path=None):
        super().__init__(path or DEFAULT_LOG_DIR)

    def _read_work_log(self):
        try:
            return (self.path / "sessions.jsonl").read_text(encoding="utf-8")
        except OSError:
            return ""

    def _read_remote_log(self, branch):
        result = self._git("show", f"origin/{branch}:sessions.jsonl")
        return result.stdout if result.returncode == 0 else ""

    @staticmethod
    def _session_ids(text):
        ids = set()
        for line in (text or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("session_id"):
                ids.add(str(record["session_id"]))
        return ids

    def sync(self, timeout=180):
        if not self._is_repo():
            raise SyncError(
                f"{self.path} is not a git repository yet; run 'git init' there first"
            )
        notes = []

        dirty = self._git("status", "--porcelain")
        if dirty.returncode != 0:
            raise SyncError(_first_line(dirty.stderr))
        if dirty.stdout.strip():
            add = self._git("add", "-A")
            if add.returncode != 0:
                raise SyncError(_first_line(add.stderr))
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            commit = self._git("commit", "-m", f"Session log {stamp}")
            if commit.returncode != 0:
                raise SyncError(_first_line(commit.stderr or commit.stdout))
            notes.append("committed local changes")

        branch = self._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if not branch or branch == "HEAD":
            raise SyncError("The log repository has no branch checked out")

        if self.has_upstream():
            fetch = self._git("fetch", "origin", branch, timeout=timeout)
            if fetch.returncode != 0:
                raise SyncError(
                    "git fetch failed: " + _first_line(fetch.stderr or fetch.stdout)
                )
            expected = self._session_ids(self._read_work_log())
            expected |= self._session_ids(self._read_remote_log(branch))
            pull = self._git(
                "pull", "--no-rebase", "--no-edit", "--allow-unrelated-histories",
                timeout=timeout,
            )
            if pull.returncode != 0:
                self._git("merge", "--abort")
                raise SyncError(
                    "git pull failed; resolve it in " + str(self.path) + ": "
                    + _first_line(pull.stderr or pull.stdout)
                )
            if "Already up to date" not in (pull.stdout or ""):
                notes.append("pulled remote changes")
            missing = expected - self._session_ids(self._read_work_log())
            if missing:
                raise SyncError(
                    "Refusing to push: "
                    + str(len(missing))
                    + " session row(s) would be lost by the merge. Nothing was "
                    + "pushed; check "
                    + str(self.path / "sessions.jsonl")
                )

        push_args = ["push"]
        if not self.has_upstream():
            push_args += ["-u", "origin", branch]
        push = self._git(*push_args, timeout=timeout)
        if push.returncode != 0:
            raise SyncError(
                "git push failed: " + _first_line(push.stderr or push.stdout)
            )
        if "Everything up-to-date" not in (push.stderr or "") + (push.stdout or ""):
            notes.append("pushed")

        return "; ".join(notes) if notes else "Already up to date"
