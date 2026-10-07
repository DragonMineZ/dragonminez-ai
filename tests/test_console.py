import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.cogs.console import ConsoleCog
from bulmaai.services.mod_actions import ActionResult, ModActionError


class ConsoleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = SimpleNamespace(id=1, get_member_named=lambda name: SimpleNamespace(id=55) if name == "steve" else None)
        bot = SimpleNamespace(
            settings=SimpleNamespace(panel_guild_id=1),
            get_guild=lambda _id: self.guild,
            get_user=lambda _id: None,
        )
        self.cog = ConsoleCog(bot)

    async def test_ban_goes_through_perform_as_console(self):
        perform = AsyncMock(return_value=ActionResult(action="ban", case_id=7))
        with patch("bulmaai.cogs.console.mod_actions.perform", perform):
            reply = await self.cog.run(["ban", "<@123>", "phishing", "links", "--delete-days", "2"])
        kwargs = perform.await_args.kwargs
        self.assertEqual(
            (kwargs["action"], kwargs["target_id"], kwargs["moderator"], kwargs["source"], kwargs["reason"]),
            ("ban", 123, None, "console", "phishing links"),
        )
        self.assertEqual(kwargs["delete_message_seconds"], 2 * 86400)
        self.assertIn("case #7", reply)

    async def test_timeout_needs_a_readable_duration_and_names_resolve(self):
        perform = AsyncMock(return_value=ActionResult(action="timeout", case_id=None))
        with patch("bulmaai.cogs.console.mod_actions.perform", perform):
            self.assertIn("Couldn't read the duration", await self.cog.run(["timeout", "123", "soon"]))
            await self.cog.run(["timeout", "steve", "2h", "spam"])
        self.assertEqual(perform.await_args.kwargs["target_id"], 55)
        self.assertEqual(perform.await_args.kwargs["duration_seconds"], 7200)

    async def test_errors_and_unknown_commands_come_back_as_text(self):
        failing = AsyncMock(side_effect=ModActionError("You can't moderate the server owner.", 403))
        with patch("bulmaai.cogs.console.mod_actions.perform", failing):
            self.assertEqual(await self.cog.run(["unban", "1"]), "You can't moderate the server owner.")
        self.assertIn("No member named", await self.cog.run(["unban", "nobody"]))
        self.assertIn("dmz-bot commands", await self.cog.run(["frobnicate"]))


if __name__ == "__main__":
    unittest.main()
