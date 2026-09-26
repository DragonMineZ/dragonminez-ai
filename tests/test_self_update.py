import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.cogs import self_update


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(repo: Path, body: str) -> None:
    (repo / "src" / "mod.py").write_text(body)
    sh(repo, "add", "-A")
    sh(repo, "commit", "-qm", body)


class ApplyUpdateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.origin = Path(self.tmp.name) / "origin"
        (self.origin / "src").mkdir(parents=True)
        sh(self.origin, "init", "-q", "-b", "main")
        commit(self.origin, "x = 1\n")
        self.clone = Path(self.tmp.name) / "clone"
        sh(Path(self.tmp.name), "clone", "-q", str(self.origin), str(self.clone))
        self.old = sh(self.clone, "rev-parse", "HEAD")
        patch.object(self_update, "REPO_ROOT", self.clone).start()
        patch.object(self_update, "IMPORT_CHECK", "import mod").start()
        self.addCleanup(patch.stopall)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_good_commit_is_pulled(self) -> None:
        commit(self.origin, "x = 2\n")
        ok, message = await self_update.apply_update()
        self.assertTrue(ok, message)
        self.assertEqual(sh(self.clone, "rev-parse", "HEAD"), sh(self.origin, "rev-parse", "HEAD"))

    async def test_broken_commit_is_rolled_back(self) -> None:
        commit(self.origin, "raise ImportError('boom')\n")
        ok, message = await self_update.apply_update()
        self.assertFalse(ok)
        self.assertIn("boom", message)
        self.assertEqual(sh(self.clone, "rev-parse", "HEAD"), self.old)

    async def test_nothing_new(self) -> None:
        ok, message = await self_update.apply_update()
        self.assertFalse(ok)
        self.assertIn("up to date", message)


if __name__ == "__main__":
    unittest.main()
