import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.cogs import support_info as cog_module
from bulmaai.services import openai_client
from bulmaai.services import support_info as db
from bulmaai.ui import support_info_views as views
from v2_helpers import text

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def entry(**overrides) -> db.SupportInfo:
    values = dict(
        id=1,
        kind="temporary",
        title="Beta 2.2.1 crashes with Iris",
        body="Use 2.2.2 instead.",
        status="active",
        source="manual",
        ticket_id=None,
        expires_at=None,
        created_by=200,
        updated_by=200,
        review_channel_id=None,
        review_message_id=None,
        created_at=NOW,
        updated_at=NOW,
    )
    values.update(overrides)
    return db.SupportInfo(**values)


def row(info: db.SupportInfo) -> dict:
    return {key: getattr(info, key) for key in db.SupportInfo.__slots__}


def pool(**returns) -> MagicMock:
    fake = MagicMock()
    fake.fetchrow = AsyncMock(return_value=returns.get("fetchrow"))
    fake.fetch = AsyncMock(return_value=returns.get("fetch", []))
    fake.execute = AsyncMock()
    return fake


class ValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_bad_input_before_touching_the_db(self) -> None:
        fake = pool()
        cases = [
            dict(kind="forever", title="t", body="b"),
            dict(kind="temporary", title="   ", body="b"),
            dict(kind="temporary", title="t", body=""),
            dict(kind="permanent", title="x" * 101, body="b"),
            dict(kind="permanent", title="t", body="x" * 1501),
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                await db.add_info(**case, created_by=1, pool=fake)
        fake.fetchrow.assert_not_awaited()

    async def test_add_strips_and_drops_expiry_for_permanent(self) -> None:
        fake = pool(fetchrow=row(entry(kind="permanent")))
        await db.add_info(
            kind="permanent", title="  Q  ", body=" A ", created_by=1, expires_at=NOW, pool=fake
        )
        args = fake.fetchrow.await_args.args
        self.assertEqual(args[2:4], ("Q", "A"))
        self.assertIsNone(args[7])

    async def test_set_status_is_conditional(self) -> None:
        fake = pool(fetchrow=None)
        self.assertIsNone(await db.set_status(1, "active", updated_by=2, pool=fake))
        query, *args = fake.fetchrow.await_args.args
        self.assertIn("status = ANY($4::text[])", query)
        self.assertEqual(args[3], ["suggested"])

    async def test_writes_drop_the_ai_cache(self) -> None:
        fake = pool(fetch=[row(entry())], fetchrow=row(entry()))
        self.assertEqual(len(await db.cached_active(pool=fake)), 1)
        await db.cached_active(pool=fake)
        self.assertEqual(fake.fetch.await_count, 1)
        await db.add_info(kind="temporary", title="t", body="b", created_by=1, pool=fake)
        await db.cached_active(pool=fake)
        self.assertEqual(fake.fetch.await_count, 2)
        db.invalidate_cache()


class RenderForAITests(unittest.TestCase):
    def test_keeps_order_and_stops_at_the_limit(self) -> None:
        entries = [entry(id=1, title="A", body="a" * 50), entry(id=2, kind="permanent", title="B", body="b" * 50)]
        rendered = db.render_for_ai(entries)
        self.assertEqual(rendered, f"### [temporary] A\n{'a' * 50}\n\n### [permanent] B\n{'b' * 50}")
        first_only = db.render_for_ai(entries, limit_chars=len("### [temporary] A\n") + 60)
        self.assertEqual(first_only, f"### [temporary] A\n{'a' * 50}")
        self.assertEqual(db.render_for_ai([]), "")


class DataInputTests(unittest.IsolatedAsyncioTestCase):
    def test_support_info_section_goes_first(self) -> None:
        data = openai_client._build_data_input(
            [{"filename": "wiki--X.md", "text": "x"}], ["requester_x: 1"], "### [temporary] A\na"
        )
        content = data[0]["content"]
        self.assertTrue(content.startswith(openai_client.SUPPORT_INFO_HEADER + "### [temporary] A"))
        self.assertLess(content.index("Support info"), content.index("Looked-up data"))
        self.assertLess(content.index("Looked-up data"), content.index("Knowledge search"))

    def test_no_section_without_entries(self) -> None:
        self.assertEqual(openai_client._build_data_input([], [], ""), [])
        content = openai_client._build_data_input([], ["a"])[0]["content"]
        self.assertNotIn("Support info", content)

    async def test_db_failure_drops_the_section(self) -> None:
        with patch.object(db, "cached_active", AsyncMock(side_effect=OSError("db down"))):
            self.assertEqual(await openai_client._support_info_text(), "")
        with patch.object(db, "cached_active", AsyncMock(return_value=[entry()])):
            self.assertIn("### [temporary] Beta 2.2.1", await openai_client._support_info_text())


class DailyCardTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_card_and_edit_modal(self) -> None:
        stamp = int(NOW.timestamp())
        body = text(views.info_list_card([entry(id=4, expires_at=NOW)], pending=2))
        self.assertIn(f"#4 · temporary · Beta 2.2.1 crashes with Iris · until <t:{stamp}:R>", body)
        self.assertIn("2 pending suggestions", body)
        modal = cog_module.InfoModal(kind="permanent", info_id=4, title="Q", body="A")
        self.assertEqual((modal.title_input.value, modal.body_input.value), ("Q", "A"))

    async def test_card_text(self) -> None:
        expiring = entry(id=2, title="Server lag", body="Restart.", expires_at=NOW + timedelta(days=3))
        body = text(views.daily_post_card([entry(), expiring]))
        stamp = int(NOW.timestamp())
        self.assertIn(views.DAILY_HEAD, body)
        self.assertIn(f"**Beta 2.2.1 crashes with Iris**\nUse 2.2.2 instead.\n-# Since <t:{stamp}:R>", body)
        self.assertIn(f"-# Since <t:{stamp}:R> · until <t:{stamp + 3 * 86400}:R>", body)
        self.assertTrue(body.endswith(views.DAILY_FOOTER))

    async def test_drops_oldest_whole_entries_to_fit(self) -> None:
        entries = [entry(id=i, title=f"T{i}", body="x" * 1500) for i in range(5)]
        body = text(views.daily_post_card(entries))
        self.assertLessEqual(len(body), 4000)
        self.assertIn("**T0**", body)
        self.assertNotIn("**T4**", body)
        self.assertNotIn("…", body)


def make_cog(channel_id=555):
    sent = SimpleNamespace(id=999)
    channel = MagicMock()
    channel.send = AsyncMock(return_value=sent)
    old_message = MagicMock()
    old_message.delete = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    channel.get_partial_message = MagicMock(return_value=old_message)
    bot = MagicMock()
    bot.settings = SimpleNamespace(
        support_info_channel_id=channel_id, discord_staff_role_ids=(7,), panel_guild_id=1, dev_guild_id=None
    )
    bot.get_partial_messageable = MagicMock(return_value=channel)
    return cog_module.SupportInfoCog(bot), channel, old_message


class DailyPostLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_posts_active_temporary_entries_without_pings(self) -> None:
        cog, channel, old_message = make_cog()
        with (
            patch.object(db, "claim_daily_post", AsyncMock(return_value=db.DailyPost(555, 111))),
            patch.object(db, "list_active", AsyncMock(return_value=[entry(), entry(id=3, kind="permanent", title="P")])),
            patch.object(db, "record_daily_post", AsyncMock()) as record,
        ):
            await cog._post_tick()
        old_message.delete.assert_awaited_once()
        kwargs = channel.send.await_args.kwargs
        allowed = kwargs["allowed_mentions"]
        self.assertFalse(allowed.everyone or allowed.users or allowed.roles or allowed.replied_user)
        posted = text(kwargs["view"])
        self.assertIn("Beta 2.2.1", posted)
        self.assertNotIn("**P**", posted)
        record.assert_awaited_once_with(555, 999)

    async def test_nothing_active_posts_nothing(self) -> None:
        cog, channel, _ = make_cog()
        with (
            patch.object(db, "claim_daily_post", AsyncMock(return_value=db.DailyPost(None, None))),
            patch.object(db, "list_active", AsyncMock(return_value=[entry(kind="permanent")])),
            patch.object(db, "record_daily_post", AsyncMock()) as record,
        ):
            await cog._post_tick()
        channel.send.assert_not_awaited()
        record.assert_awaited_once_with(555, None)

    async def test_not_due_does_nothing(self) -> None:
        cog, channel, _ = make_cog()
        with (
            patch.object(db, "claim_daily_post", AsyncMock(return_value=None)),
            patch.object(db, "list_active", AsyncMock()) as list_active,
        ):
            await cog._post_tick()
        list_active.assert_not_awaited()
        channel.send.assert_not_awaited()


class InfoCommandTests(unittest.IsolatedAsyncioTestCase):
    def ctx(self, *role_ids, guild_id=1):
        author = MagicMock(spec=discord.Member)
        author.id = 300
        author.roles = [SimpleNamespace(id=role_id) for role_id in role_ids]
        author.guild = SimpleNamespace(id=guild_id)
        context = MagicMock()
        context.author = author
        context.respond = AsyncMock()
        context.send_modal = AsyncMock()
        return context

    async def test_non_staff_are_refused(self) -> None:
        cog, _, _ = make_cog()
        for context in (self.ctx(), self.ctx(7, guild_id=42)):
            with patch.object(db, "set_status", AsyncMock()) as set_status:
                await cog.add.callback(cog, context, "temporary", 3)
                await cog.remove.callback(cog, context, 5)
            context.send_modal.assert_not_awaited()
            set_status.assert_not_awaited()
            context.respond.assert_awaited_with(cog_module.STAFF_ONLY_MSG, ephemeral=True)

    async def test_staff_add_opens_modal_with_expiry(self) -> None:
        cog, _, _ = make_cog()
        context = self.ctx(7)
        await cog.add.callback(cog, context, "temporary", 3)
        modal = context.send_modal.await_args.args[0]
        self.assertEqual(modal.kind, "temporary")
        self.assertAlmostEqual(
            (modal.expires_at - datetime.now(timezone.utc)).total_seconds(), 3 * 86400, delta=60
        )
        await cog.add.callback(cog, context, "permanent", 3)
        self.assertIsNone(context.send_modal.await_args.args[0].expires_at)

    async def test_staff_remove(self) -> None:
        cog, _, _ = make_cog()
        context = self.ctx(7)
        with patch.object(db, "set_status", AsyncMock(return_value=entry(status="removed"))) as set_status:
            await cog.remove.callback(cog, context, 1)
        self.assertEqual(set_status.await_args.kwargs["only_from"], ("active", "suggested"))
        context.respond.assert_awaited_with("Removed #1.", ephemeral=True)


if __name__ == "__main__":
    unittest.main()
