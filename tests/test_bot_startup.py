import os
import unittest
from unittest.mock import AsyncMock, patch


os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.bot import BulmaAI
from bulmaai.config import load_settings


class BotStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_runs_setup_hook_because_pycord_never_calls_it(self) -> None:
        bot = BulmaAI(load_settings(include_overrides=False))
        with (
            patch.object(BulmaAI, "setup_hook", AsyncMock()) as setup_hook,
            patch.object(discord.Bot, "start", AsyncMock()) as parent_start,
        ):
            await bot.start("token")

        setup_hook.assert_awaited_once()
        parent_start.assert_awaited_once_with("token", reconnect=True)

    async def test_setup_hook_survives_database_failure_and_installs_log_forwarder(self) -> None:
        bot = BulmaAI(load_settings(include_overrides=False))
        with (
            patch("bulmaai.bot.install_discord_log_forwarder") as install,
            patch("bulmaai.bot.init_db_pool", AsyncMock(side_effect=OSError("db down"))),
            patch("bulmaai.bot.ensure_message_presets_file"),
            self.assertLogs("bulmaai", level="ERROR"),
        ):
            await bot.setup_hook()

        install.assert_called_once()


if __name__ == "__main__":
    unittest.main()
