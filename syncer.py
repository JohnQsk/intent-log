"""Inspect and configure the git repository that dstask syncs through.

`dstask sync` is just `git pull` followed by `git push` inside ~/.dstask, so the
"sync target" is simply that repository's `origin` remote. This module reads
that state and can repoint it, without touching task data.
"""

import re
import subprocess
from pathlib import Path

DSTASK_DIR = Path.home() / ".dstask"

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


class DstaskRepo:
    def __init__(self, path=None):
        self.path = Path(path) if path else DSTASK_DIR

    def _git(self, *args, timeout=60):
        return _run(["git", "-C", str(self.path), *args], timeout=timeout)

    def _is_repo(self):
        result = self._git("rev-parse", "--is-inside-work-tree")
        return result.returncode == 0 and result.stdout.strip() == "true"

    def status(self):
        """Read-only view: remote URL, branch, dirty flag, ahead/behind."""
        if not self.path.is_dir():
            return {"git": False, "path": str(self.path), "remote": None,
                    "branch": None, "dirty": False, "ahead": None, "behind": None}
        if not self._is_repo():
            return {"git": False, "path": str(self.path), "remote": None,
                    "branch": None, "dirty": False, "ahead": None, "behind": None}

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

    def set_remote(self, url):
        url = validate_url(url)
        current = self._git("config", "--get", "remote.origin.url")
        if current.returncode == 0 and current.stdout.strip():
            result = self._git("remote", "set-url", "origin", url)
        else:
            result = self._git("remote", "add", "origin", url)
        if result.returncode != 0:
            message = (result.stderr or result.stdout).strip() or "git failed"
            raise SyncError(message.splitlines()[0])
        return self.status()

    def sync(self, timeout=180):
        """Run `dstask sync` (git pull, then git push)."""
        result = _run(["dstask", "sync"], timeout=timeout)
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0:
            raise SyncError(output or "dstask sync failed")
        return output
