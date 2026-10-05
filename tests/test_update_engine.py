import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord


os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.services import update_engine
from bulmaai.services.update_plan import UpdatePlan
from bulmaai.utils.lifecycle import ReloadableCog


COG = """
from bulmaai.utils.lifecycle import ReloadableCog
from hotpkg import helper

VERSION = {version}


class HotCog(ReloadableCog):
    def __init__(self, bot):
        self.bot = bot
        self.count = 0

    async def on_startup(self):
        if {fail_startup}:
            raise RuntimeError("startup boom")
        self.bot.events.append(("start", VERSION, helper.VALUE))

    async def on_shutdown(self):
        self.bot.events.append(("stop", VERSION))

    def export_state(self):
        return self.count

    def import_state(self, state):
        self.count = state + 1


def setup(bot):
    bot.add_cog(HotCog(bot))
"""


def make_bot() -> discord.Bot:
    bot = discord.Bot(intents=discord.Intents.none())
    bot.events = []
    bot.reload_state = {}
    bot.update_lock = asyncio.Lock()
    bot.settings = SimpleNamespace(panel_enabled=False)
    bot.panel_server = None
    return bot


class EngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.pkg = Path(self.tmp.name) / "hotpkg"
        self.pkg.mkdir()
        (self.pkg / "__init__.py").write_text("")
        self.write(version=1, value=10)
        sys.path.insert(0, self.tmp.name)
        self.addCleanup(sys.path.remove, self.tmp.name)
        self.addCleanup(lambda: [sys.modules.pop(name) for name in list(sys.modules) if name.startswith("hotpkg")])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, *, version: int, value: int, fail_startup: bool = False) -> None:
        (self.pkg / "cog.py").write_text(COG.format(version=version, fail_startup=fail_startup))
        (self.pkg / "helper.py").write_text(f"VALUE = {value}\n")
        # Same-second rewrites can keep a stale .pyc; drop them so the reload reads the new source.
        for cached in self.pkg.glob("__pycache__/*"):
            cached.unlink()

    def plan(self) -> UpdatePlan:
        return UpdatePlan(mode="hot", modules_to_reload=["hotpkg.helper"], extensions_to_reload=["hotpkg.cog"])

    async def boot(self) -> discord.Bot:
        bot = make_bot()
        bot.load_extension("hotpkg.cog")
        await bot.get_cog("HotCog").start_lifecycle()
        bot._ready.set()  # from here on, (re)loaded cogs start themselves
        return bot

    async def test_hot_reload_swaps_code_and_carries_state(self) -> None:
        bot = await self.boot()
        bot.get_cog("HotCog").count = 5
        self.write(version=2, value=20)

        await update_engine.reload_in_process(bot, self.plan(), ["hotpkg.cog"])

        self.assertEqual(bot.events, [("start", 1, 10), ("stop", 1), ("start", 2, 20)])
        self.assertEqual(bot.get_cog("HotCog").count, 6)

    async def test_failed_startup_rolls_back_to_the_old_code(self) -> None:
        bot = await self.boot()
        self.write(version=2, value=20, fail_startup=True)

        async def fake_git(*args, cwd=None):
            if args[0] == "reset":
                self.write(version=1, value=10)
            return ""

        with patch.object(update_engine, "git", AsyncMock(side_effect=fake_git)), self.assertLogs(level="ERROR"):
            result = await update_engine.apply_hot(bot, self.plan(), "old", "new")

        self.assertFalse(result.ok)
        self.assertTrue(result.rolled_back)
        self.assertFalse(result.restart_needed)
        self.assertIn("startup boom", result.message)
        self.assertIn("hotpkg.cog", bot.extensions)
        self.assertEqual(bot.events[-1], ("start", 1, 10))

    async def test_rollback_that_also_fails_asks_for_a_restart(self) -> None:
        bot = await self.boot()
        self.write(version=2, value=20, fail_startup=True)
        with patch.object(update_engine, "git", AsyncMock(return_value="")), self.assertLogs(level="ERROR"):
            result = await update_engine.apply_hot(bot, self.plan(), "old", "new")
        self.assertTrue(result.restart_needed)

    async def test_busy_cog_is_waited_for(self) -> None:
        bot = await self.boot()
        cog = bot.get_cog("HotCog")
        busy = [True, True, False]
        cog.is_busy = lambda: busy.pop(0) if busy else False
        self.write(version=2, value=20)
        with patch.object(update_engine.asyncio, "sleep", AsyncMock()) as sleep:
            await update_engine.reload_in_process(bot, self.plan(), ["hotpkg.cog"])
        self.assertEqual(sleep.await_count, 2)

    async def test_manual_reload_keeps_old_code_when_the_new_one_wont_import(self) -> None:
        bot = await self.boot()
        (self.pkg / "cog.py").write_text("raise ImportError('nope')\n")
        for cached in self.pkg.glob("__pycache__/*"):
            cached.unlink()
        with self.assertRaises(discord.ExtensionError):
            await update_engine.reload_extension_safely(bot, "hotpkg.cog")
        self.assertIsNotNone(bot.get_cog("HotCog"))
        self.assertEqual(bot.events[-1], ("start", 1, 10))


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_and_stop_run_once(self) -> None:
        calls = []

        class Cog(ReloadableCog):
            def __init__(self, bot):
                self.bot = bot

            async def on_startup(self):
                calls.append("start")

            async def on_shutdown(self):
                calls.append("stop")

        cog = Cog(SimpleNamespace(reload_state={}))
        await cog.start_lifecycle()
        await cog.start_lifecycle()
        await cog.stop_lifecycle()
        await cog.stop_lifecycle()
        self.assertEqual(calls, ["start", "stop"])


if __name__ == "__main__":
    unittest.main()
