import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.cogs.github_cmds import GitHubCog, _build_issue_embed
from bulmaai.config import load_settings
from bulmaai.ui.github_views import MergeConfirmView

SETTINGS = load_settings(include_overrides=False)
HELPER_ROLE = 1341595261960589343  # staff, panel helper tier
MAIN_GUILD = SimpleNamespace(id=SETTINGS.panel_guild_id, owner_id=0)


def member(user_id, role_ids=()):
    return SimpleNamespace(id=user_id, guild=MAIN_GUILD, roles=[SimpleNamespace(id=r) for r in role_ids])


def ctx(author):
    return SimpleNamespace(author=author, respond=AsyncMock(), defer=AsyncMock(side_effect=AssertionError("deferred")))


class GitHubCommandTests(unittest.IsolatedAsyncioTestCase):  # py-cord Views need a running loop
    def setUp(self):
        self.cog = GitHubCog(SimpleNamespace(settings=SETTINGS))

    async def test_staff_below_panel_admin_cannot_merge(self):
        context = ctx(member(5, [HELPER_ROLE]))
        await self.cog.merge_pr.callback(self.cog, context, 12)
        self.assertIn("Only admins", context.respond.await_args.args[0])

    async def test_unknown_repo_is_refused_privately(self):
        context = ctx(member(5))
        await self.cog.view_issue.callback(self.cog, context, 1, "someone-elses-repo")
        self.assertTrue(context.respond.await_args.kwargs["ephemeral"])

    async def test_merge_controls_only_answer_the_command_author(self):
        view = MergeConfirmView(author_id=1)
        interaction = SimpleNamespace(user=SimpleNamespace(id=2), response=SimpleNamespace(send_message=AsyncMock()))
        self.assertFalse(await view.interaction_check(interaction))
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])

    def test_issue_body_masked_links_do_not_render(self):
        issue = {"number": 1, "title": "t", "html_url": "u", "state": "open", "body": "[free nitro](https://evil.example)"}
        self.assertEqual(_build_issue_embed(issue, "o", "r").description, r"\[free nitro](https://evil.example)")


if __name__ == "__main__":
    unittest.main()
