import asyncio
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.cogs import self_update
from bulmaai.services import update_engine
from bulmaai.services.update_plan import UpdatePlan


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
        patch.object(update_engine, "REPO_ROOT", self.clone).start()
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


class HandleUpdateTests(unittest.IsolatedAsyncioTestCase):
    def make_cog(self, *, auto_apply=True):
        bot = SimpleNamespace(
            settings=SimpleNamespace(self_update_auto_apply=auto_apply),
            extensions={},
            update_lock=asyncio.Lock(),
        )
        cog = self_update.SelfUpdateCog(bot)
        cog._send = AsyncMock()
        cog._prompt_restart = AsyncMock()
        return cog

    def patches(self, plan, checks=(True, "ok")):
        for target, value in (
            ("git", AsyncMock(return_value="abc commit")),
            ("plan_update", MagicMock(return_value=plan)),
        ):
            patch.object(self_update, target, value).start()
        engine = {
            "changed_paths": AsyncMock(return_value=[]),
            "prepare_staging": AsyncMock(return_value=Path("/staging")),
            "is_dirty": AsyncMock(return_value=False),
            "run_checks": AsyncMock(return_value=checks),
            "apply_hot": AsyncMock(return_value=update_engine.HotResult(ok=True, message="")),
            "record_update": AsyncMock(),
        }
        for name, value in engine.items():
            patch.object(update_engine, name, value).start()
        self.addCleanup(patch.stopall)
        return engine

    async def test_full_plan_asks_bruno(self):
        cog = self.make_cog()
        engine = self.patches(UpdatePlan(mode="full", reasons=["requirements.txt changed"]))
        await cog._handle_update("old", "new")
        cog._prompt_restart.assert_awaited_once()
        engine["run_checks"].assert_not_awaited()
        engine["apply_hot"].assert_not_awaited()

    async def test_hot_plan_is_checked_then_applied(self):
        cog = self.make_cog()
        plan = UpdatePlan(mode="hot", extensions_to_reload=["bulmaai.cogs.meta"], tests=["test_meta"])
        engine = self.patches(plan)
        await cog._handle_update("old", "new")
        engine["run_checks"].assert_awaited_once_with(Path("/staging"), plan)
        engine["apply_hot"].assert_awaited_once()
        cog._prompt_restart.assert_not_awaited()

    async def test_failed_checks_never_touch_the_live_bot(self):
        cog = self.make_cog()
        engine = self.patches(UpdatePlan(mode="hot", extensions_to_reload=["x"]), checks=(False, "tests failed"))
        await cog._handle_update("old", "new")
        engine["apply_hot"].assert_not_awaited()
        cog._prompt_restart.assert_awaited_once()

    async def test_auto_apply_off_falls_back_to_the_button(self):
        cog = self.make_cog(auto_apply=False)
        engine = self.patches(UpdatePlan(mode="hot", extensions_to_reload=["x"]))
        await cog._handle_update("old", "new")
        engine["apply_hot"].assert_not_awaited()
        cog._prompt_restart.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
