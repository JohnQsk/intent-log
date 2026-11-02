"""Tests for the dstask sync configuration helpers."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from syncer import DstaskRepo, GitRepo, LogRepo, SyncError, validate_url  # noqa: E402

GIT = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com"]


class SyncConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.repo = DstaskRepo(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.path), *args],
            capture_output=True, text=True, encoding="utf-8",
        )

    def make_repo(self, remote=None):
        self.git("init", "-q", "-b", "master")
        (self.path / "pending").mkdir(exist_ok=True)
        (self.path / "pending" / "task.yml").write_text("summary: hi\n", encoding="utf-8")
        subprocess.run(
            [*GIT, "-C", str(self.path), "add", "-A"], capture_output=True, text=True
        )
        subprocess.run(
            [*GIT, "-C", str(self.path), "commit", "-q", "-m", "init"],
            capture_output=True, text=True,
        )
        if remote:
            self.git("remote", "add", "origin", remote)

    def test_missing_repository_is_reported(self):
        status = self.repo.status()
        self.assertFalse(status["git"])
        self.assertIsNone(status["remote"])

    def test_status_reads_remote_and_branch(self):
        self.make_repo("git@github.com:someone/tasks.git")
        status = self.repo.status()
        self.assertTrue(status["git"])
        self.assertEqual(status["remote"], "git@github.com:someone/tasks.git")
        self.assertEqual(status["branch"], "master")
        self.assertFalse(status["dirty"])

    def test_set_remote_replaces_existing(self):
        self.make_repo("git@github.com:someone/tasks.git")
        status = self.repo.set_remote("https://github.com/someone/tasks.git")
        self.assertEqual(status["remote"], "https://github.com/someone/tasks.git")

    def test_set_remote_adds_when_missing(self):
        self.make_repo()
        self.assertIsNone(self.repo.status()["remote"])
        status = self.repo.set_remote("git@github.com:someone/tasks.git")
        self.assertEqual(status["remote"], "git@github.com:someone/tasks.git")

    def test_dirty_flag(self):
        self.make_repo("https://example.com/tasks.git")
        (self.path / "pending" / "other.yml").write_text("summary: x\n", encoding="utf-8")
        self.assertTrue(self.repo.status()["dirty"])

    def test_validate_url(self):
        good = [
            "https://github.com/someone/tasks.git",
            "http://127.0.0.1:3000/tasks.git",
            "git@github.com:someone/tasks.git",
            "ssh://git@host/someone/tasks.git",
        ]
        for url in good:
            self.assertEqual(validate_url(url), url)
        bad = [
            "",
            "   ",
            "ftp://host/x",
            "https://ok; rm -rf ~",
            "-oProxyCommand=calc.exe",
            "git@host:a b:x",
            "not a url",
        ]
        for url in bad:
            with self.assertRaises(SyncError, msg=url):
                validate_url(url)

    def test_set_remote_rejects_bad_url_without_changing_anything(self):
        self.make_repo("https://example.com/tasks.git")
        with self.assertRaises(SyncError):
            self.repo.set_remote("javascript:alert(1)")
        self.assertEqual(self.repo.status()["remote"], "https://example.com/tasks.git")


class LogRepoTests(unittest.TestCase):
    """The session log repository: commit local rows, rebase, push."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.origin = self.root / "origin.git"
        subprocess.run(
            ["git", "init", "--bare", "-q", str(self.origin)],
            capture_output=True, text=True,
        )
        self.work = self.root / "log"
        self.work.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("remote", "add", "origin", str(self.origin))
        (self.work / ".gitattributes").write_text(
            "sessions.jsonl merge=union\n", encoding="utf-8"
        )
        (self.work / "sessions.jsonl").write_text(
            '{"session_id": "a"}\n', encoding="utf-8"
        )
        self.repo = LogRepo(self.work)

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(
            [*GIT, "-C", str(self.work), *args],
            capture_output=True, text=True, encoding="utf-8",
        )

    def remote_log(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.origin), *args],
            capture_output=True, text=True, encoding="utf-8",
        )

    def test_sync_commits_and_pushes(self):
        message = self.repo.sync()
        self.assertIn("committed local changes", message)
        self.assertIn("pushed", message)
        self.assertEqual(self.remote_log("rev-list", "--count", "main").stdout.strip(), "1")
        content = self.remote_log("show", "main:sessions.jsonl").stdout
        self.assertIn('"a"', content)

    def test_second_sync_is_a_noop(self):
        self.repo.sync()
        self.assertEqual(self.repo.sync(), "Already up to date")

    def test_status_without_upstream(self):
        status = self.repo.status()
        self.assertTrue(status["git"])
        self.assertIsNone(status["ahead"])
        self.assertTrue(status["dirty"])
        self.repo.sync()
        status = self.repo.status()
        self.assertEqual(status["ahead"], 0)
        self.assertEqual(status["behind"], 0)
        self.assertFalse(status["dirty"])

    def test_not_a_repository(self):
        with self.assertRaises(SyncError):
            LogRepo(self.root / "nothing").sync()

    def test_pulls_other_device_rows_before_pushing(self):
        self.repo.sync()

        other = self.root / "other"
        subprocess.run(
            ["git", "clone", "-q", "--branch", "main", str(self.origin), str(other)],
            capture_output=True, text=True,
        )
        (other / ".gitattributes").write_text(
            "sessions.jsonl merge=union\n", encoding="utf-8"
        )
        with (other / "sessions.jsonl").open("a", encoding="utf-8") as handle:
            handle.write('{"session_id": "b"}\n')
        subprocess.run([*GIT, "-C", str(other), "add", "-A"], capture_output=True)
        subprocess.run(
            [*GIT, "-C", str(other), "commit", "-q", "-m", "other device"],
            capture_output=True, text=True,
        )
        subprocess.run(
            [*GIT, "-C", str(other), "push", "-q", "origin", "main"],
            capture_output=True, text=True,
        )

        with (self.work / "sessions.jsonl").open("a", encoding="utf-8") as handle:
            handle.write('{"session_id": "c"}\n')
        self.repo.sync()

        lines = (self.work / "sessions.jsonl").read_text(encoding="utf-8").splitlines()
        joined = "\n".join(lines)
        self.assertIn('"b"', joined, joined)
        self.assertIn('"c"', joined, joined)

    def test_conflict_is_reported_and_nothing_is_pushed(self):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        self.git("push", "-u", "origin", "main")

        other = self.root / "other"
        subprocess.run(
            ["git", "clone", "-q", "--branch", "main", str(self.origin), str(other)],
            capture_output=True, text=True,
        )
        # Drop the union driver so the two appends really conflict.
        (other / ".gitattributes").unlink()
        with (other / "sessions.jsonl").open("a", encoding="utf-8") as handle:
            handle.write('{"session_id": "b"}\n')
        subprocess.run([*GIT, "-C", str(other), "add", "-A"], capture_output=True)
        subprocess.run(
            [*GIT, "-C", str(other), "commit", "-q", "-m", "other device"],
            capture_output=True, text=True,
        )
        subprocess.run(
            [*GIT, "-C", str(other), "push", "-q", "origin", "main"],
            capture_output=True, text=True,
        )

        (self.work / ".gitattributes").unlink()
        with (self.work / "sessions.jsonl").open("a", encoding="utf-8") as handle:
            handle.write('{"session_id": "c"}\n')

        with self.assertRaises(SyncError) as caught:
            self.repo.sync()
        self.assertIn("git pull failed", str(caught.exception))

        # the remote still has only the first row: nothing was force-pushed
        remote = self.remote_log("show", "main:sessions.jsonl").stdout
        self.assertNotIn('"c"', remote)
        # and the local repository is not left mid-merge
        self.assertEqual(
            self.git("status", "--porcelain", "--untracked-files=no").stdout.strip(),
            "",
        )



if __name__ == "__main__":
    unittest.main()
