"""Tests for the dstask sync configuration helpers."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from syncer import DstaskRepo, SyncError, validate_url  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
