import os
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.cogs import ai_daily as cog_module
from bulmaai.services import ai_daily
from bulmaai.ui.ai_daily_views import daily_card
from v2_helpers import texts, walk

DAY = date(2026, 11, 12)  # a Thursday
STAFF_ROLE = 7


def settings(**overrides):
    values = dict(
        ai_monthly_budget_usd=100,
        ai_budget_cycle_day=1,
        ai_daily_small_token_limit=1000,
        ai_daily_big_token_limit=1000,
        discord_staff_role_ids=(STAFF_ROLE,),
        panel_guild_id=1,
        dev_guild_id=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def row(day, **kw):
    return dict(day=day, **kw)


def busy_data(**overrides):
    previous = [row(date(2026, 11, d), workflow="support_question", reply_kind="answer", n=100) for d in range(5, 12)]
    args = dict(
        answer_rows=previous
        + [
            row(DAY, workflow="support_question", reply_kind="answer", n=120),
            row(DAY, workflow="support_escalation", reply_kind="answer", n=13),
            row(DAY, workflow="support_question", reply_kind="handoff", n=9),
        ],
        cost_rows=[row(DAY, feature="support_question", cost=1.84)],
        opened_rows=[row(DAY, n=31)],
        closed_rows=[row(DAY, closed=28, resolved=20)],
        feedback_rows=[row(DAY, helpful=True, n=14), row(DAY, helpful=False, n=3)],
        pool_rows=[row(DAY, model="claude-haiku-5-5", tokens=640), row(DAY, model="claude-sonnet-5-5", tokens=220)],
        cycle_spend=23.10,
        suggestions_waiting=3,
        answers_unreviewed=5,
    )
    args.update(overrides)
    return ai_daily.aggregate(DAY, settings(), **args)


class CardTests(unittest.IsolatedAsyncioTestCase):
    async def test_busy_day_lines(self) -> None:
        view = daily_card(busy_data(), eval_line="Last eval: 39% pass (13 Nov)")
        blocks = texts(view)
        self.assertEqual(blocks[0], "## 📊 AI daily · Thu 12 Nov")
        lines = blocks[1].split("\n")
        self.assertEqual(len(lines), 4)
        self.assertEqual(
            lines[0], "**Answers** 142 (+42% vs 7-day avg) · handed off 6% · second opinion 9% · 👍 14 👎 3"
        )
        self.assertEqual(lines[1], "**Tickets** opened 31 · closed 28 · resolved 71%")
        self.assertTrue(lines[2].startswith("**Cost** $1.84 · cycle $23.10 of $100 (on pace for $"))
        self.assertTrue(lines[2].endswith(" · Haiku pool 64% · Sonnet pool 22%"))
        self.assertEqual(lines[3], "**Waiting on staff** 3 support info suggestions · 5 👎 answers")
        self.assertEqual(blocks[-1], "-# Last eval: 39% pass (13 Nov)")

    async def test_chart_points_at_the_attachment(self) -> None:
        galleries = [i for i in walk(daily_card(busy_data())) if isinstance(i, discord.ui.MediaGallery)]
        self.assertEqual(len(galleries), 1)
        self.assertEqual(galleries[0].items[0].url, "attachment://ai-daily.png")

    async def test_zeros_drop_pieces(self) -> None:
        quiet = settings(ai_monthly_budget_usd=0, ai_daily_small_token_limit=None, ai_daily_big_token_limit=None)
        data = ai_daily.aggregate(DAY, quiet, cost_rows=[row(DAY, feature="support_question", cost=0.5)], cycle_spend=2)
        blocks = texts(daily_card(data))
        self.assertEqual(blocks[1].split("\n"), ["**Answers** 0", "**Cost** $0.50 · cycle $2.00"])
        self.assertEqual(len(blocks), 2)  # no footer

    async def test_partial_title(self) -> None:
        data = busy_data()
        partial = ai_daily.DailyData(**{**data.__dict__, "partial": True})
        self.assertEqual(texts(daily_card(partial))[0], "## 📊 AI daily · today so far")

    async def test_es_and_pt_render(self) -> None:
        for language in ("es", "pt"):
            blocks = texts(daily_card(busy_data(), eval_line="x", language=language))
            self.assertIn("12", blocks[0])
            self.assertEqual(len(blocks[1].split("\n")), 4)


def make_cog(**setting_overrides):
    bot = MagicMock()
    bot.settings = settings(**setting_overrides)
    return cog_module.AIDailyCog(bot)


def staff_member(*role_ids, guild_id=1):
    member = MagicMock(spec=discord.Member)
    member.id = 300
    member.roles = [SimpleNamespace(id=role_id) for role_id in role_ids]
    member.guild = SimpleNamespace(id=guild_id)
    return member


class CogTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_is_staff_only(self) -> None:
        cog = make_cog()
        for member in (staff_member(99), staff_member(STAFF_ROLE, guild_id=2), SimpleNamespace(id=1)):
            ctx = MagicMock()
            ctx.author = member
            ctx.respond = AsyncMock()
            ctx.defer = AsyncMock()
            await cog_module.AIDailyCog.ai_daily_command.callback(cog, ctx)
            ctx.respond.assert_awaited_once()
            self.assertTrue(ctx.respond.call_args.kwargs["ephemeral"])
            ctx.defer.assert_not_called()

    async def test_command_sends_ephemeral_card(self) -> None:
        cog = make_cog()
        ctx = MagicMock()
        ctx.author = staff_member(STAFF_ROLE)
        ctx.defer = AsyncMock()
        ctx.followup.send = AsyncMock()
        with (
            patch.object(ai_daily, "collect", AsyncMock(return_value=busy_data())),
            patch.object(cog_module, "render_chart", MagicMock(return_value=b"png")),
            patch.object(cog_module, "_last_eval", MagicMock(return_value=None)),
        ):
            await cog_module.AIDailyCog.ai_daily_command.callback(cog, ctx)
        ctx.defer.assert_awaited_once_with(ephemeral=True)
        self.assertTrue(ctx.followup.send.call_args.kwargs["ephemeral"])
        self.assertEqual(ctx.followup.send.call_args.kwargs["file"].filename, "ai-daily.png")

    def test_no_scheduled_post(self) -> None:
        self.assertFalse(hasattr(cog_module.AIDailyCog, "daily_post"))


if __name__ == "__main__":
    unittest.main()
