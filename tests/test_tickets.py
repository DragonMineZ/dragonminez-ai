import json
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.cogs import tickets as cog_module
from bulmaai.cogs.tickets import InlineImageHandler, Rank, TicketsCog, build_overwrites, member_rank
from bulmaai.services import tickets as db
from bulmaai.services.ticket_pages import StoredPage
from bulmaai.services.ticket_intake import triage_intake
from bulmaai.ui import ticket_views as views

OWNER_ROLE, ADMIN_ROLE, MOD_ROLE, HELPER_ROLE, TESTER_ROLE = 11, 12, 13, 14, 15
OWNER_ID, STAFF_ID = 100, 200

SETTINGS = SimpleNamespace(
    panel_owner_role_ids=(OWNER_ROLE,),
    panel_admin_role_ids=(ADMIN_ROLE,),
    panel_moderator_role_ids=(MOD_ROLE,),
    panel_helper_role_ids=(HELPER_ROLE,),
    ticket_tester_role_ids=(TESTER_ROLE,),
    ticket_dm_transcript=True,
    ticket_max_open_per_user=2,
    ticket_transcript_public_url="https://tickets.example",
    ai_ticket_category_id=1,
    ticket_closed_category_id=2,
    openai_ticket_summary_model="gpt-5-mini",
)


class Snow:
    """Hashable stand-in for a Discord role/member (SimpleNamespace can't be a dict key)."""

    def __init__(self, id):
        self.id = id


def member(user_id, *role_ids):
    return SimpleNamespace(id=user_id, roles=[SimpleNamespace(id=role_id) for role_id in role_ids])


def make_ticket(**overrides):
    values = dict(
        ticket_id=42,
        guild_id=1,
        owner_id=OWNER_ID,
        channel_id=900,
        category="bug",
        status="open",
        channel_name="crash-0042",
        language="en",
        control_message_id=77,
        claimed_by=None,
        closed_by=None,
        close_reason=None,
        created_at=None,
        closed_at=None,
    )
    values.update(overrides)
    return db.Ticket(**values)


class SlugTests(unittest.TestCase):
    def test_slug_is_discord_safe(self):
        self.assertEqual(db.sanitize_slug("Crash on Start-up!! 💥", "bug"), "crash-on-start-up")
        self.assertEqual(db.sanitize_slug("¿Qué pasó? Ñandú", "bug"), "que-paso-nandu")
        self.assertEqual(db.sanitize_slug("💥💥", "bug"), "bug")
        self.assertEqual(db.sanitize_slug(None, "help"), "help")
        self.assertLessEqual(len(db.sanitize_slug("a" * 30 + "-" + "b" * 30, "x")), 40)
        self.assertFalse(db.sanitize_slug("a" * 39 + "-bbbb", "x").endswith("-"))

    def test_names_round_trip_the_number(self):
        self.assertEqual(db.open_channel_name("crash", 42), "crash-0042")
        self.assertEqual(db.closed_channel_name(42), "closed-0042")


class RankTests(unittest.TestCase):
    def rank(self, user, owner_id=OWNER_ID):
        return member_rank(user, owner_id, SETTINGS)

    def test_staff_tiers(self):
        for role in (OWNER_ROLE, ADMIN_ROLE, MOD_ROLE):
            self.assertEqual(self.rank(member(STAFF_ID, role)), Rank.MOD)
        self.assertEqual(self.rank(member(STAFF_ID, HELPER_ROLE)), Rank.HELPER)

    def test_tester_is_read_only_unless_owner_or_staff(self):
        self.assertEqual(self.rank(member(STAFF_ID, TESTER_ROLE)), Rank.TESTER)
        self.assertEqual(self.rank(member(OWNER_ID, TESTER_ROLE)), Rank.OWNER)
        self.assertEqual(self.rank(member(STAFF_ID, TESTER_ROLE, HELPER_ROLE)), Rank.HELPER)
        self.assertEqual(self.rank(member(STAFF_ID)), Rank.NONE)

    def test_only_owner_and_up_may_close(self):
        self.assertLess(Rank.TESTER, Rank.OWNER)
        self.assertLess(Rank.NONE, Rank.OWNER)


class OverwriteTests(unittest.TestCase):
    def test_permission_matrix(self):
        roles = {role_id: Snow(role_id) for role_id in (OWNER_ROLE, ADMIN_ROLE, MOD_ROLE, HELPER_ROLE, TESTER_ROLE)}
        guild = SimpleNamespace(default_role=Snow(1), me=Snow(2), get_role=roles.get)
        owner = Snow(OWNER_ID)
        overwrites = build_overwrites(guild, owner, SETTINGS)

        self.assertIs(overwrites[guild.default_role].view_channel, False)

        o = overwrites[owner]
        self.assertEqual((o.view_channel, o.send_messages, o.read_message_history, o.attach_files), (True,) * 4)

        t = overwrites[roles[TESTER_ROLE]]
        self.assertEqual((t.view_channel, t.read_message_history), (True, True))
        self.assertEqual((t.send_messages, t.add_reactions), (False, False))

        for role_id in (OWNER_ROLE, ADMIN_ROLE, MOD_ROLE):
            m = overwrites[roles[role_id]]
            self.assertEqual((m.view_channel, m.send_messages, m.manage_messages, m.read_message_history), (True,) * 4)
        h = overwrites[roles[HELPER_ROLE]]
        self.assertEqual((h.view_channel, h.send_messages, h.read_message_history), (True,) * 3)
        self.assertIs(h.manage_messages, False)

    def test_missing_roles_are_skipped(self):
        guild = SimpleNamespace(default_role=Snow(1), me=Snow(2), get_role=lambda _id: None)
        overwrites = build_overwrites(guild, Snow(OWNER_ID), SETTINGS)
        self.assertEqual(len(overwrites), 3)


class FormTests(unittest.IsolatedAsyncioTestCase):
    async def test_modals_fit_discord_limits(self):
        self.assertEqual({"bug", "contribute", "other"}, set(views.CATEGORIES))
        for category in views.CATEGORIES.values():
            modal = views.TicketModal(category)
            self.assertLessEqual(len(modal.title), 45)
            self.assertLessEqual(len(modal.children), 5)
            self.assertLessEqual(len(category.label), 100)
            self.assertLessEqual(len(category.description), 100)
            for field, child in zip(category.fields, modal.children):
                self.assertLessEqual(len(child.label), 45, child.label)
                self.assertLessEqual(len(child.item.placeholder), 100, child.item.placeholder)
                self.assertIs(child.item.required, field.required)
                # EN | ES, plus PT only where it differs from Spanish
                self.assertIn(field.label.count(" | "), (1, 2), field.label)

    async def test_bug_form_optional_fields(self):
        optional = {field.label.split(" | ")[0] for field in views.CATEGORIES["bug"].fields if not field.required}
        self.assertEqual(optional, {"Steps to reproduce", "Other mods"})
        payload = views.TicketModal(views.CATEGORIES["bug"]).to_dict()["components"]
        self.assertEqual([c["type"] for c in payload], [discord.ComponentType.label.value] * 5)

    async def test_persistent_views_have_fixed_custom_ids(self):
        ids = lambda view: [child.custom_id for child in view.children]
        self.assertEqual(ids(views.TicketPanelView()), [views.PANEL_SELECT_ID])
        self.assertEqual(ids(views.TicketControlView()), ["ticket_btn_close", views.AI_BUTTON_ID])
        self.assertEqual(
            ids(views.TicketClosedView()), ["ticket_btn_transcript", "ticket_btn_reopen", "ticket_btn_delete"]
        )
        for view in (views.TicketPanelView(), views.TicketControlView(), views.TicketClosedView()):
            self.assertIsNone(view.timeout)

    async def test_ticket_embed_has_no_ai_summary_and_warns_about_pings(self):
        embed = views.build_ticket_embed(number=7, category=views.CATEGORIES["bug"], answers=[("Summary", "boom")])
        self.assertEqual(embed.title, "🎫 #0007 · ⚙️ Game-Breaking Bug")
        self.assertIn("ping staff", embed.description)
        self.assertEqual([field.name for field in embed.fields], ["Summary"])
        self.assertEqual(views.TicketControlView().children[0].label, "Close | Cerrar")


class FakeConn:
    def __init__(self, last, active):
        self.last, self.active, self.executed, self.rows = last, active, [], []

    def transaction(self):
        conn = self

        class Tx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return Tx()

    async def fetchval(self, query, *args):
        return self.last if "ticket_counter" in query else self.active

    async def execute(self, query, *args):
        self.executed.append((query, args))

    async def fetchrow(self, query, *args):
        self.rows.append(args)
        keys = db.Ticket.__slots__
        values = dict(zip(keys, [args[0], args[1], args[2], None, args[3], "creating", None, args[4], None, None, None, None, None, None]))
        return values


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return Ctx()


class NumberingTests(unittest.IsolatedAsyncioTestCase):
    async def test_reserve_takes_next_number(self):
        conn = FakeConn(last=41, active=0)
        ticket = await db.reserve_ticket(
            guild_id=1, owner_id=OWNER_ID, category="bug", language="es", max_open=2, pool=FakePool(conn)
        )
        self.assertEqual(ticket.ticket_id, 42)
        self.assertEqual(conn.executed[0][1], (42,))

    async def test_reserve_refuses_at_the_limit_without_burning_a_number(self):
        conn = FakeConn(last=41, active=2)
        ticket = await db.reserve_ticket(
            guild_id=1, owner_id=OWNER_ID, category="bug", language="en", max_open=2, pool=FakePool(conn)
        )
        self.assertIsNone(ticket)
        self.assertEqual(conn.executed, [])


class InlineImageTests(unittest.IsolatedAsyncioTestCase):
    def attachment(self, content_type, size, data=b"abc"):
        return SimpleNamespace(
            content_type=content_type, size=size, url="https://cdn/x", proxy_url="https://cdn/x", read=AsyncMock(return_value=data)
        )

    async def test_small_images_become_data_uris(self):
        handler = InlineImageHandler(budget=100)
        image = await handler.process_asset(self.attachment("image/png", 3))
        self.assertEqual(image.url, "data:image/png;base64,YWJj")
        self.assertEqual(handler.budget, 97)

    async def test_everything_else_keeps_its_link(self):
        handler = InlineImageHandler(budget=100)
        for content_type, size in (("text/plain", 3), ("image/svg+xml", 3), ("image/png", 101), (None, 3)):
            item = self.attachment(content_type, size)
            self.assertEqual((await handler.process_asset(item)).url, "https://cdn/x")
            item.read.assert_not_called()


class FakeOpenAI:
    def __init__(self, payload):
        self.responses = SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(output_text=json.dumps(payload))))


class IntakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_intake_sanitizes_model_output(self):
        client = FakeOpenAI(
            {"channel_slug": "@everyone Crash!!", "language": "fr", "summary": "See https://evil.example and @everyone"}
        )
        intake = await triage_intake(
            "Bug", [("Summary", "boom")], model="gpt-5-mini", fallback_slug="bug", openai_client=client
        )
        self.assertEqual(intake.channel_slug, "everyone-crash")
        self.assertEqual(intake.language, "en")
        self.assertNotIn("evil.example", intake.summary)
        self.assertNotIn("@everyone", intake.summary)
        self.assertEqual(client.responses.create.await_args.kwargs["model"], "gpt-5-mini")


def make_cog():
    bot = SimpleNamespace(settings=SETTINGS, get_cog=lambda name: None, get_channel=lambda _id: None, user=SimpleNamespace(id=9))
    return TicketsCog(bot)


def make_channel(owner=None):
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 900
    channel.name = "crash-0042"
    channel.guild = SimpleNamespace(
        id=1, name="DragonMine Z", icon=None, get_member=lambda _id: owner, get_channel=lambda _id: None, filesize_limit=10_000_000, default_role=object()
    )
    channel.overwrites_for.return_value = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    channel.set_permissions = AsyncMock()
    channel.edit = AsyncMock()
    channel.send = AsyncMock()
    channel.delete = AsyncMock()
    channel.get_partial_message.return_value = SimpleNamespace(edit=AsyncMock())
    return channel


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_locks_owner_moves_renames_and_posts_closed_embed(self):
        cog, owner, channel = make_cog(), member(OWNER_ID), make_channel()
        channel.guild.get_member = lambda _id: owner
        closed_category = MagicMock(spec=discord.CategoryChannel)
        closed_category.id = SETTINGS.ticket_closed_category_id
        channel.guild.get_channel = lambda _id: closed_category if _id == closed_category.id else None
        with (
            patch.object(cog_module, "mark_closed", AsyncMock(return_value=make_ticket(status="closed"))) as closed,
            patch.object(cog, "archive", AsyncMock()) as archive,
        ):
            ticket = await cog.close_ticket(channel, closer_id=STAFF_ID, reason="done")
        self.assertEqual(ticket.ticket_id, 42)
        closed.assert_awaited_once_with(900, closed_by=STAFF_ID, reason="done")
        archive.assert_not_called()  # transcripts are on request only
        overwrite = channel.set_permissions.await_args.kwargs["overwrite"]
        self.assertIs(overwrite.send_messages, False)
        self.assertIs(overwrite.view_channel, True)
        self.assertEqual(
            [call.kwargs for call in channel.edit.await_args_list], [{"category": closed_category}, {"name": "closed-0042"}]
        )
        self.assertIsNone(channel.get_partial_message.return_value.edit.await_args.kwargs["view"])
        sent = channel.send.await_args.kwargs
        self.assertIn("<@200>", sent["embed"].description)
        self.assertEqual(
            [c.custom_id for c in sent["view"].children],
            ["ticket_btn_transcript", "ticket_btn_reopen", "ticket_btn_delete"],
        )

    async def test_closing_twice_does_nothing(self):
        cog, channel = make_cog(), make_channel()
        with patch.object(cog_module, "mark_closed", AsyncMock(return_value=None)):
            self.assertIsNone(await cog.close_ticket(channel, closer_id=STAFF_ID, reason=None))
        channel.set_permissions.assert_not_called()
        channel.edit.assert_not_called()

    async def test_reopen_restores_owner_name_and_close_button(self):
        cog, owner, channel = make_cog(), member(OWNER_ID), make_channel()
        channel.guild.get_member = lambda _id: owner
        channel.overwrites_for.return_value = discord.PermissionOverwrite(view_channel=True, send_messages=False)
        with patch.object(cog_module, "mark_reopened", AsyncMock(return_value=make_ticket())):
            await cog.reopen_ticket(channel, opener_id=STAFF_ID)
        self.assertIs(channel.set_permissions.await_args.kwargs["overwrite"].send_messages, True)
        channel.edit.assert_awaited_once_with(name="crash-0042")
        view = channel.get_partial_message.return_value.edit.await_args.kwargs["view"]
        self.assertEqual([c.custom_id for c in view.children], ["ticket_btn_close", "ticket_btn_ai"])

    async def test_member_leaving_closes_and_deletes(self):
        cog, channel = make_cog(), make_channel()
        guild = SimpleNamespace(id=1, get_channel=lambda _id: channel)
        leaver = SimpleNamespace(id=OWNER_ID, guild=guild)
        ticket = make_ticket()
        with (
            patch.object(cog_module, "get_open_tickets_by_owner", AsyncMock(return_value=[ticket])),
            patch.object(cog, "close_ticket", AsyncMock(return_value=ticket)) as close,
            patch.object(cog, "delete_ticket", AsyncMock(return_value=True)) as delete,
        ):
            await cog.on_member_remove(leaver)
        close.assert_awaited_once_with(channel, closer_id=None, reason="User left the server.")
        delete.assert_awaited_once_with(channel, ticket)

    async def test_other_guild_tickets_are_left_alone(self):
        cog, channel = make_cog(), make_channel()
        guild = SimpleNamespace(id=2, get_channel=lambda _id: channel)
        with (
            patch.object(cog_module, "get_open_tickets_by_owner", AsyncMock(return_value=[make_ticket()])),
            patch.object(cog, "close_ticket", AsyncMock()) as close,
        ):
            await cog.on_member_remove(SimpleNamespace(id=OWNER_ID, guild=guild))
        close.assert_not_called()

    async def test_reopen_lets_the_ticket_be_archived_again(self):
        ai_cog = SimpleNamespace(forget_archived=MagicMock(), is_ticket_ai_enabled=lambda _id: True)
        cog, channel = make_cog(), make_channel()
        cog.bot.get_cog = lambda name: ai_cog if name == "AITicketsCog" else None
        with patch.object(cog_module, "mark_reopened", AsyncMock(return_value=make_ticket())):
            await cog.reopen_ticket(channel, opener_id=STAFF_ID)
        ai_cog.forget_archived.assert_called_once_with(900)

    async def test_archive_dms_the_owner_an_embed_with_a_link_button(self):
        owner = SimpleNamespace(id=OWNER_ID, roles=[], send=AsyncMock())
        cog, channel = make_cog(), make_channel(owner)
        page = StoredPage(token="a" * 32, expires_at=datetime(2026, 11, 2, tzinfo=timezone.utc))
        with (
            patch.object(cog, "_archive", AsyncMock(return_value=True)),
            patch.object(cog_module, "get_page_for_channel", AsyncMock(return_value=page)),
        ):
            self.assertTrue(await cog.archive(channel, make_ticket(), closed_by_id=STAFF_ID))
        kwargs = owner.send.await_args.kwargs
        self.assertIn("<t:", kwargs["embed"].description)
        self.assertEqual(kwargs["view"].children[0].url, f"{SETTINGS.ticket_transcript_public_url}/t/{'a' * 32}")

    async def test_archive_without_a_page_or_that_failed_sends_no_dm(self):
        owner = SimpleNamespace(id=OWNER_ID, roles=[], send=AsyncMock())
        cog, channel = make_cog(), make_channel(owner)
        for archived, page in ((True, None), (False, StoredPage(token="a" * 32, expires_at=None))):
            with (
                patch.object(cog, "_archive", AsyncMock(return_value=archived)),
                patch.object(cog_module, "get_page_for_channel", AsyncMock(return_value=page)),
            ):
                await cog.archive(channel, make_ticket(), closed_by_id=STAFF_ID)
        owner.send.assert_not_called()

    async def test_build_page_hosts_the_export(self):
        cog, channel = make_cog(), make_channel()
        page = StoredPage(token="b" * 32, expires_at=None)
        with (
            patch.object(cog, "export_html", AsyncMock(return_value=b"<html/>")),
            patch.object(cog_module, "save_page", AsyncMock(return_value=page)) as save,
        ):
            self.assertIs(await cog.build_page(channel), page)
        save.assert_awaited_once_with(SETTINGS, b"<html/>")
        with patch.object(cog, "export_html", AsyncMock(return_value=None)):
            self.assertIsNone(await cog.build_page(channel))

    async def test_delete_just_deletes(self):
        cog, channel = make_cog(), make_channel()
        with (
            patch.object(cog, "archive", AsyncMock()) as archive,
            patch.object(cog_module, "mark_deleted", AsyncMock()) as deleted,
        ):
            self.assertTrue(await cog.delete_ticket(channel, make_ticket()))
        archive.assert_not_called()
        deleted.assert_awaited_once_with(900)
        channel.delete.assert_awaited_once()
        self.assertEqual(cog._deleting, set())

    async def test_second_delete_click_is_ignored(self):
        cog, channel = make_cog(), make_channel()
        cog._deleting.add(900)
        with patch.object(cog_module, "mark_deleted", AsyncMock()) as deleted:
            self.assertFalse(await cog.delete_ticket(channel, make_ticket()))
        deleted.assert_not_called()


class TranscriptPolishTests(unittest.TestCase):
    HTML = (
        '<!doctype html><html><head><title>DragonMine Z <img class="emoji"> - closed-0003</title>\n'
        '<meta property="og:description" content="Server: DragonMine Z <img src=x>" />\n<style>a{}</style></head>'
        '<body><div class="meta__support">    <a href="https://ko-fi.com/mahtoid">DONATE</a></div></body></html>'
    )

    def test_preview_meta_is_clean_and_donate_becomes_patreon(self):
        html = cog_module.polish_transcript(
            self.HTML, title='Ticket #0003 · "DMZ"', description="⚙️ Bug · 3 messages", image="https://cdn/icon.png"
        )
        head = html.split("<style>")[0]
        self.assertNotIn("<img", head)
        self.assertIn('<meta property="og:title" content="Ticket #0003 · &quot;DMZ&quot;" />', head)
        self.assertIn('<meta property="og:image" content="https://cdn/icon.png" />', head)
        self.assertIn("<style>a{}</style>", html)
        self.assertNotIn("ko-fi", html)
        self.assertIn('href="https://www.patreon.com/DragonMineZ"', html)


class StaffPingTests(unittest.IsolatedAsyncioTestCase):
    def message(self, author, *, users=(), roles=()):
        staff = member(STAFF_ID, HELPER_ROLE)
        guild = SimpleNamespace(get_member=lambda _id: staff if _id == STAFF_ID else member(_id))
        message = MagicMock()
        message.author = MagicMock(spec=discord.Member)
        message.author.bot, message.author.id, message.author.roles = False, author.id, author.roles
        message.author.mention = f"<@{author.id}>"
        message.raw_mentions, message.raw_role_mentions = list(users), list(roles)
        message.channel = SimpleNamespace(id=900, category_id=SETTINGS.ai_ticket_category_id)
        message.guild = guild
        message.reply = AsyncMock()
        return message

    async def run_listener(self, message):
        cog = make_cog()
        with (
            patch.object(cog_module, "get_ticket_by_channel", AsyncMock(return_value=make_ticket())),
            patch.object(cog_module.mod_actions, "perform", AsyncMock()) as perform,
        ):
            await cog.on_message(message)
        return perform

    async def test_member_pinging_staff_is_warned(self):
        for kwargs in ({"users": [STAFF_ID]}, {"roles": [MOD_ROLE]}):
            message = self.message(member(OWNER_ID), **kwargs)
            perform = await self.run_listener(message)
            self.assertEqual(perform.await_args.kwargs["action"], "warn")
            self.assertEqual(perform.await_args.kwargs["target_id"], OWNER_ID)
            message.reply.assert_awaited_once()

    async def test_no_warn_for_non_staff_pings_or_staff_authors(self):
        for message in (
            self.message(member(OWNER_ID), users=[555]),
            self.message(member(STAFF_ID, HELPER_ROLE), users=[STAFF_ID]),
            self.message(member(OWNER_ID)),
        ):
            (await self.run_listener(message)).assert_not_called()


class InteractionGateTests(unittest.IsolatedAsyncioTestCase):
    def interaction(self, user):
        return SimpleNamespace(
            user=user,
            channel_id=900,
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(), edit_message=AsyncMock()),
        )

    async def test_testers_cannot_use_any_button(self):
        cog = make_cog()
        tester = member(STAFF_ID, TESTER_ROLE)
        with patch.object(cog_module, "get_ticket_by_channel", AsyncMock(return_value=make_ticket())), \
                patch.object(cog, "archive", AsyncMock()) as archive, \
                patch.object(cog, "close_ticket", AsyncMock()) as close, \
                patch.object(cog, "reopen_ticket", AsyncMock()) as reopen, \
                patch.object(cog, "delete_ticket", AsyncMock()) as delete:
            for handler in (cog.on_transcript, cog.on_close, cog.on_reopen, cog.on_delete):
                interaction = self.interaction(tester)
                await handler(interaction)
                interaction.response.send_message.assert_awaited_once()
                self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])
        for action in (archive, close, reopen, delete):
            action.assert_not_called()

    async def test_owner_can_close_but_not_reopen_or_transcript(self):
        cog = make_cog()
        owner = member(OWNER_ID)
        with patch.object(cog_module, "get_ticket_by_channel", AsyncMock(return_value=make_ticket())):
            with patch.object(cog, "close_ticket", AsyncMock()) as close:
                interaction = self.interaction(owner)
                interaction.channel = make_channel()
                await cog.on_close(interaction)
                close.assert_awaited_once_with(interaction.channel, closer_id=OWNER_ID, reason=None)
        with patch.object(cog_module, "get_ticket_by_channel", AsyncMock(return_value=make_ticket(status="closed"))):
            for handler in (cog.on_reopen, cog.on_transcript):
                interaction = self.interaction(owner)
                await handler(interaction)
                interaction.response.send_message.assert_awaited_once()

    async def test_helper_gets_public_transcript_link_and_button_is_disabled(self):
        cog = make_cog()
        interaction = self.interaction(member(STAFF_ID, HELPER_ROLE))
        interaction.channel = make_channel()
        interaction.message = SimpleNamespace(edit=AsyncMock())
        interaction.followup = SimpleNamespace(send=AsyncMock())
        page = StoredPage(token="c" * 32, expires_at=None)
        with (
            patch.object(cog_module, "get_ticket_by_channel", AsyncMock(return_value=make_ticket(status="closed", closed_by=OWNER_ID))),
            patch.object(cog, "archive", AsyncMock(return_value=True)) as archive,
            patch.object(cog_module, "get_page_for_channel", AsyncMock(return_value=page)),
        ):
            await cog.on_transcript(interaction)
        archive.assert_awaited_once()
        self.assertEqual(archive.await_args.kwargs["closed_by_id"], OWNER_ID)
        self.assertIn("c" * 32, interaction.followup.send.await_args.args[0])
        self.assertNotIn("ephemeral", interaction.followup.send.await_args.kwargs)
        interaction.response.defer.assert_awaited_once_with()
        view = interaction.message.edit.await_args.kwargs["view"]
        self.assertTrue(view.get_item("ticket_btn_transcript").disabled)
        self.assertFalse(view.get_item("ticket_btn_reopen").disabled)

    async def test_reopen_strips_the_closed_embed_buttons(self):
        cog = make_cog()
        interaction = self.interaction(member(STAFF_ID, MOD_ROLE))
        interaction.guild = SimpleNamespace(get_member=lambda _id: member(_id))
        interaction.channel = make_channel()
        with (
            patch.object(cog_module, "get_ticket_by_channel", AsyncMock(return_value=make_ticket(status="closed"))),
            patch.object(cog, "reopen_ticket", AsyncMock()) as reopen,
        ):
            await cog.on_reopen(interaction)
        interaction.response.edit_message.assert_awaited_once_with(view=None)
        reopen.assert_awaited_once()

    async def test_helper_cannot_delete_but_mod_can(self):
        cog = make_cog()
        closed = make_ticket(status="closed")
        with patch.object(cog_module, "get_ticket_by_channel", AsyncMock(return_value=closed)), \
                patch.object(cog, "delete_ticket", AsyncMock(return_value=True)) as delete:
            interaction = self.interaction(member(STAFF_ID, HELPER_ROLE))
            await cog.on_delete(interaction)
            delete.assert_not_called()
            interaction = self.interaction(member(STAFF_ID, MOD_ROLE))
            interaction.channel = make_channel()
            interaction.followup = SimpleNamespace(send=AsyncMock())
            await cog.on_delete(interaction)
            delete.assert_awaited_once()

    async def test_open_ticket_cannot_be_reopened_or_deleted_via_stale_buttons(self):
        cog = make_cog()
        with patch.object(cog_module, "get_ticket_by_channel", AsyncMock(return_value=make_ticket())):
            interaction = self.interaction(member(STAFF_ID, MOD_ROLE))
            await cog.on_delete(interaction)
            interaction.response.send_message.assert_awaited_once()
            self.assertEqual(interaction.response.send_message.await_args.args[0], views.MSG["not_closed"])


class CreateTicketTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_at_limit_gets_a_message_and_no_channel(self):
        cog = make_cog()
        guild = MagicMock()
        interaction = SimpleNamespace(
            guild=guild,
            user=MagicMock(spec=discord.Member),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        with patch.object(cog_module, "reserve_ticket", AsyncMock(return_value=None)):
            await cog.create_ticket(interaction, views.CATEGORIES["bug"], [("Summary", "boom")])
        guild.create_text_channel.assert_not_called()
        self.assertEqual(interaction.edit_original_response.await_args.kwargs["content"], views.msg_limit(2))

    async def test_failed_creation_frees_the_slot(self):
        cog = make_cog()
        guild = MagicMock()
        guild.get_channel.return_value = None  # category missing
        interaction = SimpleNamespace(
            guild=guild,
            user=MagicMock(spec=discord.Member),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        with (
            patch.object(cog_module, "reserve_ticket", AsyncMock(return_value=make_ticket(channel_id=None, status="creating"))),
            patch.object(cog_module, "abandon_ticket", AsyncMock()) as abandon,
            patch.object(cog_module.ai_budget, "is_paused", return_value=True),
            self.assertLogs(cog_module.log, "ERROR"),
        ):
            await cog.create_ticket(interaction, views.CATEGORIES["bug"], [("Summary", "boom")])
        abandon.assert_awaited_once_with(42)
        self.assertEqual(interaction.edit_original_response.await_args.kwargs["content"], views.MSG["failed"])


class TypingContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class AnswerNewTicketTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from bulmaai.cogs.ai_tickets import AITicketsCog

        settings = SimpleNamespace(ai_support_enabled=True)
        self.cog = AITicketsCog(SimpleNamespace(settings=settings))
        self.cog._send_messages_with_typing = AsyncMock(return_value=True)
        self.cog._ping_escalation_roles = AsyncMock()
        self.cog._mark_ticket_escalated = AsyncMock()
        self.channel = SimpleNamespace(id=900, typing=lambda: TypingContext())
        self.member = SimpleNamespace(id=OWNER_ID, display_name="Steve (roles: staff)")

    async def answer(self, result):
        with (
            patch("bulmaai.cogs.ai_tickets.ai_budget.is_paused", return_value=False),
            patch("bulmaai.cogs.ai_tickets.run_support_agent", AsyncMock(return_value=result)) as agent,
        ):
            await self.cog.answer_new_ticket(
                self.channel, self.member, category_label="Bug", form_text="Summary: boom", language="en"
            )
        return agent

    async def test_form_becomes_the_requesters_first_message(self):
        agent = await self.answer({"reply": "Try updating.", "tool_results": [], "kind": "answer"})
        kwargs = agent.await_args.kwargs
        self.assertEqual(kwargs["messages"][0]["content"], "Summary: boom")
        self.assertEqual(kwargs["messages"][0]["speaker_kind"], "requester")
        self.assertNotIn("(", kwargs["messages"][0]["speaker_name"])
        self.assertTrue(kwargs["ticket_conversation"])
        self.assertEqual(self.cog._ticket_owners[900], OWNER_ID)
        self.cog._send_messages_with_typing.assert_awaited_once_with(self.channel, ["Try updating."])
        self.cog._mark_ticket_escalated.assert_not_called()

    async def test_handoff_escalates_and_pings_staff(self):
        await self.answer({"reply": "Staff will help.", "tool_results": [], "kind": "handoff"})
        self.cog._mark_ticket_escalated.assert_awaited_once_with(900)
        self.cog._ping_escalation_roles.assert_awaited_once_with(self.channel, OWNER_ID)

    async def test_silent_when_there_is_nothing_to_say(self):
        await self.answer({"reply": "(no reply)", "tool_results": [], "kind": None})
        self.cog._send_messages_with_typing.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class AiToggleTests(unittest.IsolatedAsyncioTestCase):
    async def test_button_shows_state_and_toggle_flips_it_publicly(self):
        on, off = views.TicketControlView(True), views.TicketControlView(False)
        self.assertEqual((on.children[1].label, off.children[1].label), ("AI support: On", "AI support: Off"))
        cog = make_cog()
        ai_cog = SimpleNamespace(is_ticket_ai_enabled=lambda _id: True, set_ticket_ai_enabled=AsyncMock())
        cog.bot.get_cog = lambda name: ai_cog if name == "AITicketsCog" else None
        interaction = SimpleNamespace(
            channel_id=900,
            user=SimpleNamespace(id=STAFF_ID, roles=[SimpleNamespace(id=HELPER_ROLE)], mention=f"<@{STAFF_ID}>"),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with patch.object(cog_module, "get_ticket_by_channel", AsyncMock(return_value=make_ticket())):
            await cog.on_toggle_ai(interaction)
        ai_cog.set_ticket_ai_enabled.assert_awaited_once_with(900, False)
        self.assertIn("AI support is now **off**", interaction.followup.send.await_args.args[0])
