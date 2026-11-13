import os
import unittest
from datetime import date, datetime, timezone
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
        ai_reports_channel_id=900,
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


def make_cog(channel_found=True, **setting_overrides):
    channel = MagicMock()
    channel.send = AsyncMock()
    bot = MagicMock()
    bot.settings = settings(**setting_overrides)
    bot.get_channel = MagicMock(return_value=channel if channel_found else None)
    return cog_module.AIDailyCog(bot), channel


def staff_member(*role_ids, guild_id=1):
    member = MagicMock(spec=discord.Member)
    member.id = 300
    member.roles = [SimpleNamespace(id=role_id) for role_id in role_ids]
    member.guild = SimpleNamespace(id=guild_id)
    return member


NOW = datetime(2026, 11, 13, 0, 30, tzinfo=timezone.utc)


class CogTests(unittest.IsolatedAsyncioTestCase):
    async def tick(self, cog, *, now=NOW, claim=True, data=None):
        with (
            patch.object(ai_daily, "collect", AsyncMock(return_value=data or busy_data())) as collect,
            patch.object(ai_daily, "claim", AsyncMock(return_value=claim)) as claim_mock,
            patch.object(ai_daily, "release", AsyncMock()) as release,
            patch.object(cog_module, "render_chart", MagicMock(return_value=b"png")),
            patch.object(cog_module, "_last_eval", MagicMock(return_value=None)),
        ):
            try:
                await cog._post_tick(now)
            except RuntimeError:
                pass
        return collect, claim_mock, release

    async def test_posts_once_with_file_and_no_pings(self) -> None:
        cog, channel = make_cog()
        _, claim, release = await self.tick(cog)
        claim.assert_awaited_once_with(date(2026, 11, 12))
        channel.send.assert_awaited_once()
        kwargs = channel.send.call_args.kwargs
        self.assertEqual(kwargs["file"].filename, "ai-daily.png")
        self.assertEqual(kwargs["allowed_mentions"].to_dict(), discord.AllowedMentions.none().to_dict())
        release.assert_not_awaited()

    async def test_lost_claim_does_nothing(self) -> None:
        cog, channel = make_cog()
        collect, _, _ = await self.tick(cog, claim=False)
        collect.assert_not_awaited()
        channel.send.assert_not_awaited()

    async def test_no_activity_posts_nothing_and_keeps_the_claim(self) -> None:
        cog, channel = make_cog()
        _, _, release = await self.tick(cog, data=ai_daily.aggregate(DAY, settings()))
        channel.send.assert_not_awaited()
        release.assert_not_awaited()

    async def test_generic_error_releases_the_claim(self) -> None:
        cog, channel = make_cog()
        channel.send.side_effect = RuntimeError("boom")
        _, _, release = await self.tick(cog)
        release.assert_awaited_once_with(date(2026, 11, 12))

    async def test_forbidden_keeps_the_claim(self) -> None:
        cog, channel = make_cog()
        channel.send.side_effect = discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "no")
        _, _, release = await self.tick(cog)
        release.assert_not_awaited()

    async def test_disabled_too_early_or_missing_channel(self) -> None:
        cog, _ = make_cog(ai_reports_channel_id=None)
        self.assertFalse((await self.tick(cog))[1].await_count)
        cog, _ = make_cog()
        early = datetime(2026, 11, 13, 0, 5, tzinfo=timezone.utc)
        self.assertFalse((await self.tick(cog, now=early))[1].await_count)
        cog, _ = make_cog(channel_found=False)
        self.assertFalse((await self.tick(cog))[1].await_count)

    async def test_command_is_staff_only(self) -> None:
        cog, _ = make_cog()
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
        cog, _ = make_cog()
        ctx = MagicMock()
        ctx.author = staff_member(STAFF_ROLE)
        ctx.defer = AsyncMock()
        ctx.followup.send = AsyncMock()
        with (
            patch.object(ai_daily, "collect", AsyncMock(return_value=busy_data())),
            patch.object(ai_daily, "claim", AsyncMock()) as claim,
            patch.object(cog_module, "render_chart", MagicMock(return_value=b"png")),
            patch.object(cog_module, "_last_eval", MagicMock(return_value=None)),
        ):
            await cog_module.AIDailyCog.ai_daily_command.callback(cog, ctx)
        ctx.defer.assert_awaited_once_with(ephemeral=True)
        self.assertTrue(ctx.followup.send.call_args.kwargs["ephemeral"])
        self.assertEqual(ctx.followup.send.call_args.kwargs["file"].filename, "ai-daily.png")
        claim.assert_not_called()


if __name__ == "__main__":
    unittest.main()
