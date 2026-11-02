import os
import unittest
from types import SimpleNamespace

import discord

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.cogs.meta import BRUNO, help_sections, required_level
from bulmaai.web.core import Tier


async def _noop(ctx):
    pass


def slash(name, description="does things", **kwargs):
    return discord.SlashCommand(_noop, name=name, description=description, **kwargs)


def commands():
    ticket = discord.SlashCommandGroup("ticket", "Ticket tools")
    for name in ("close", "add", "panel"):
        async def sub(ctx):
            pass

        ticket.command(name=name, description="does things")(sub)
    return [
        slash("ask", "Ask the AI."),
        slash("warn", default_member_permissions=discord.Permissions(moderate_members=True)),
        slash("ban", default_member_permissions=discord.Permissions(moderate_members=True)),
        slash("lock", "Stop @everyone from talking", default_member_permissions=discord.Permissions(moderate_members=True)),
        slash("database"),
        slash("devonly", guild_ids=[99]),
        ticket,
    ]


class HelpTests(unittest.TestCase):
    def test_required_level(self):
        by_name = {c.qualified_name: c for c in commands()}
        self.assertEqual(required_level(by_name["ask"]), Tier.NONE)
        self.assertEqual(required_level(by_name["warn"]), Tier.HELPER)
        self.assertEqual(required_level(by_name["ban"]), Tier.MODERATOR)
        self.assertEqual(required_level(by_name["database"]), BRUNO)
        close, add, panel = by_name["ticket"].subcommands
        self.assertEqual([required_level(c) for c in (close, add, panel)], [Tier.NONE, Tier.HELPER, Tier.MODERATOR])

    def test_members_only_see_their_commands(self):
        text = "\n".join(help_sections(commands(), Tier.NONE, 1))
        self.assertIn("`/ask` Ask the AI", text)
        self.assertIn("`/ticket` close", text)
        for hidden in ("/warn", "/ban", "/database", "add", "panel", "/devonly", "Staff"):
            self.assertNotIn(hidden, text)

    def test_moderators_get_staff_sections(self):
        sections = help_sections(commands(), Tier.MODERATOR, 1)
        self.assertEqual([s.splitlines()[0] for s in sections], ["### 🌐 Everyone", "### 🛡️ Staff", "### 🔨 Moderators"])
        self.assertIn("`/lock` Stop `@everyone` from talking", sections[2])
        self.assertNotIn("/database", "\n".join(sections))
        self.assertIn("/devonly", "\n".join(help_sections(commands(), BRUNO, 99)))


if __name__ == "__main__":
    unittest.main()
