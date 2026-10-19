import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.cogs import tickets as cog_module
from bulmaai.cogs.tickets import InlineImageHandler, Rank, TicketsCog, build_overwrites, member_rank
from bulmaai.services import tickets as db
from bulmaai.services.ticket_pages import StoredPage
from bulmaai.services.ticket_intake import TicketIntake, triage_intake
from bulmaai.utils.language import role_languages
from bulmaai.ui import ticket_views as views
from bulmaai.ui import v2
from v2_helpers import sent_message

OWNER_ROLE, ADMIN_ROLE, MOD_ROLE, HELPER_ROLE, TESTER_ROLE = 11, 12, 13, 14, 15
EN_ROLE, ES_ROLE, PT_ROLE = 21, 22, 23
OWNER_ID, STAFF_ID = 100, 200

SETTINGS = SimpleNamespace(
    panel_owner_role_ids=(OWNER_ROLE,),
    panel_admin_role_ids=(ADMIN_ROLE,),
    panel_moderator_role_ids=(MOD_ROLE,),
    panel_helper_role_ids=(HELPER_ROLE,),
    ticket_dm_transcript=True,
    ticket_transcript_public_url="https://tickets.example",
    ai_ticket_category_id=1,
    ticket_closed_category_id=2,
    ai_ticket_summary_model="gpt-5-mini",
    announcement_role_en_id=EN_ROLE,
    announcement_role_es_id=ES_ROLE,
    announcement_role_pt_id=PT_ROLE,
)


def walk(item):
    yield item
    for child in getattr(item, "items", None) or getattr(item, "children", None) or []:
        yield from walk(child)


def texts(view):
    return "\n".join(item.content for item in walk(view) if isinstance(item, discord.ui.TextDisplay))


def custom_ids(view):
    return [item.custom_id for item in walk(view) if getattr(item, "custom_id", None)]


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

    def test_non_staff_non_owner_has_no_rank(self):
        self.assertEqual(self.rank(member(OWNER_ID)), Rank.OWNER)
        self.assertEqual(self.rank(member(STAFF_ID)), Rank.NONE)

    def test_only_owner_and_up_may_close(self):
        self.assertLess(Rank.NONE, Rank.OWNER)


class OverwriteTests(unittest.TestCase):
    def test_permission_matrix(self):
        roles = {role_id: Snow(role_id) for role_id in (OWNER_ROLE, ADMIN_ROLE, MOD_ROLE, HELPER_ROLE)}
        guild = SimpleNamespace(default_role=Snow(1), me=Snow(2), get_role=roles.get)
        owner = Snow(OWNER_ID)
        overwrites = build_overwrites(guild, owner, SETTINGS)

        self.assertIs(overwrites[guild.default_role].view_channel, False)

        o = overwrites[owner]
        self.assertEqual((o.view_channel, o.send_messages, o.read_message_history, o.attach_files), (True,) * 4)

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

    async def test_cards_have_fixed_custom_ids(self):
        self.assertEqual(custom_ids(views.panel_view()), [views.PANEL_SELECT_ID])
        self.assertEqual(custom_ids(views.closed_view(None, None)), ["ticket_btn_transcript", "ticket_btn_reopen", "ticket_btn_delete"])
        for row in (views.control_row(), views.closed_row()):
            self.assertEqual(row.id, views.ACTIONS_ID)

    async def test_welcome_card_shows_the_request_apart_and_warns_about_pings(self):
        view = views.welcome_view(
            number=7,
            category=views.CATEGORIES["bug"],
            answers=[("Summary", "boom"), ("Version", "2.2")],
            mention="<@1>",
            language="en",
        )
        greeting, request, lang_row, row = view.children
        self.assertIn("## 🎫 #0007 · ⚙️ Game-Breaking Bug", texts(greeting))
        self.assertIn("ping staff", texts(greeting))
        self.assertNotIn("¡Hola", texts(greeting))  # one language only
        self.assertIn("<@1>", texts(greeting))
        self.assertEqual(texts(request), "-# Summary\n### boom\n**Version**\n2.2")
        self.assertEqual((lang_row.id, custom_ids(lang_row)), (views.LANG_ROW_ID, [views.LANG_SELECT_ID]))
        self.assertEqual(custom_ids(row), [views.AI_BUTTON_ID, "ticket_btn_close", views.SOLVED_BUTTON_ID])
        self.assertIsNone(view.timeout)

    async def test_welcome_card_greets_in_the_ticket_language_and_selects_it(self):
        view = views.welcome_view(number=7, category=views.CATEGORIES["bug"], answers=[], mention="<@1>", language="pt")
        self.assertIn("Olá <@1>!", texts(view.children[0]))
        self.assertIn("Não marque a staff", texts(view.children[0]))
        select = view.get_item(views.LANG_ROW_ID).children[0]
        self.assertEqual([(o.value, o.default) for o in select.options], [("en", False), ("es", False), ("pt", True)])

    async def test_relanguage_welcome_rewrites_the_greeting_and_select_default(self):
        posted = views.welcome_view(
            number=7, category=views.CATEGORIES["bug"], answers=[("Summary", "boom")], mention="<@1>", language="en"
        )
        view = views.relanguage_welcome(
            discord.ui.DesignerView.from_message(sent_message(posted), timeout=None),
            number=7,
            category=views.CATEGORIES["bug"],
            mention="<@1>",
            language="es",
        )
        self.assertIn("¡Hola <@1>!", texts(view.children[0]))
        self.assertEqual(texts(view.children[1]), "-# Summary\n### boom")
        select = view.get_item(views.LANG_ROW_ID).children[0]
        self.assertEqual([o.value for o in select.options if o.default], ["es"])
        self.assertEqual(custom_ids(view.get_item(views.ACTIONS_ID)), [views.AI_BUTTON_ID, "ticket_btn_close", views.SOLVED_BUTTON_ID])

    async def test_member_dms_are_in_the_ticket_language(self):
        channel = SimpleNamespace(guild=SimpleNamespace(icon=None), jump_url="https://discord.com/channels/1/900")
        later = datetime(2099, 1, 1, tzinfo=timezone.utc)
        cases = (
            (views.dm_created(42, views.CATEGORIES["bug"], channel, "es"), "Tu ticket ha sido creado", "Abrir ticket"),
            (views.dm_transcript(42, channel.guild, "https://t/x", None, "pt"), "Aqui está a transcrição", "Ver transcrição"),
            (views.dm_solved(42, channel.guild, "https://t/x", None, later, "es"), "se eliminará", "Ver transcripción"),
            (views.dm_reminder(42, channel, "es"), "⏰ Ticket #0042: tu ticket espera tu respuesta", "Abrir ticket"),
        )
        for view, text, label in cases:
            self.assertIn(text, texts(view))
            self.assertNotIn(" | ", texts(view))
            self.assertIn(label, [item.label for item in walk(view) if isinstance(item, discord.ui.Button)])

    async def test_language_picker_asks_in_each_of_the_owners_languages(self):
        view = views.language_picker("<@1>", ["es", "pt"])
        self.assertIn("<@1>", texts(view))
        self.assertIn("¿En qué idioma", texts(view))
        self.assertIn("Em qual idioma", texts(view))
        self.assertEqual(custom_ids(view), ["ticket_lang:es", "ticket_lang:pt"])


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
        values = dict(zip(keys, [args[0], args[1], args[2], None, args[3], "creating", None, args[4], None, None, None, None, None, None, [], None]))
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
            guild_id=1, owner_id=OWNER_ID, category="bug", language="es", pool=FakePool(conn)
        )
        self.assertEqual(ticket.ticket_id, 42)
        self.assertEqual(conn.executed[0][1], (42,))

    async def test_reserve_refuses_at_the_limit_without_burning_a_number(self):
        conn = FakeConn(last=41, active=1)
        ticket = await db.reserve_ticket(
            guild_id=1, owner_id=OWNER_ID, category="bug", language="en", pool=FakePool(conn)
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
        response = SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(payload))], stop_reason="end_turn")
        self.messages = SimpleNamespace(create=AsyncMock(return_value=response))


class IntakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_intake_sanitizes_model_output(self):
        client = FakeOpenAI(
            {"channel_slug": "@everyone Crash!!", "language": "fr", "summary": "See https://evil.example and @everyone"}
        )
        intake = await triage_intake(
            "Bug", [("Summary", "boom")], model="claude-haiku-5-5", fallback_slug="bug", client=client
        )
        self.assertEqual(intake.channel_slug, "everyone-crash")
        self.assertEqual(intake.language, "en")
        self.assertNotIn("evil.example", intake.summary)
        self.assertNotIn("@everyone", intake.summary)
        kwargs = client.messages.create.await_args.kwargs
        self.assertEqual(kwargs["model"], "claude-haiku-5-5")
        self.assertEqual(kwargs["messages"][0]["role"], "user")
        self.assertEqual(kwargs["output_config"]["format"]["type"], "json_schema")


def make_cog():
    bot = SimpleNamespace(settings=SETTINGS, get_cog=lambda name: None, get_channel=lambda _id: None, user=SimpleNamespace(id=9))
    return TicketsCog(bot)


def make_channel(owner=None):
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 900
    channel.name = "crash-0042"
    channel.guild = SimpleNamespace(
        id=1, name="DragonMine Z", icon=None, me=Snow(9), get_role=lambda _id: None, get_member=lambda _id: owner, get_channel=lambda _id: None, filesize_limit=10_000_000, default_role=object()
    )
    channel.overwrites_for.return_value = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    channel.set_permissions = AsyncMock()
    channel.edit = AsyncMock()
    channel.send = AsyncMock()
    channel.delete = AsyncMock()
    channel.fetch_message = AsyncMock(return_value="welcome")
    return channel


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_locks_owner_moves_renames_and_posts_closed_card(self):
        cog, owner, channel = make_cog(), Snow(OWNER_ID), make_channel()
        channel.guild.get_member = lambda _id: owner
        closed_category = MagicMock(spec=discord.CategoryChannel)
        closed_category.id = SETTINGS.ticket_closed_category_id
        channel.guild.get_channel = lambda _id: closed_category if _id == closed_category.id else None
        ai_cog = SimpleNamespace(ticket_closed=AsyncMock())
        cog.bot.get_cog = lambda name: ai_cog if name == "AITicketsCog" else None
        with (
            patch.object(cog_module, "mark_closed", AsyncMock(return_value=make_ticket(status="closed"))) as closed,
            patch.object(cog, "archive", AsyncMock()) as archive,
            patch.object(cog, "_set_row", AsyncMock()) as set_row,
        ):
            ticket = await cog.close_ticket(channel, closer_id=STAFF_ID, reason="done")
        self.assertEqual(ticket.ticket_id, 42)
        closed.assert_awaited_once_with(900, closed_by=STAFF_ID, reason="done", delete_at=None)
        ai_cog.ticket_closed.assert_awaited_once_with(900)
        edits = [call.kwargs for call in channel.edit.await_args_list]
        self.assertNotIn(owner, edits[0]["overwrites"])  # staff only from here on
        self.assertEqual(edits[1:], [{"category": closed_category}, {"name": "closed-0042"}])
        set_row.assert_awaited_once_with("welcome", None)
        sent = channel.send.await_args.kwargs["view"]
        self.assertIn("<@200>", texts(sent))
        self.assertEqual(custom_ids(sent), ["ticket_btn_transcript", "ticket_btn_reopen", "ticket_btn_delete"])
        saving = next(item for item in walk(sent) if getattr(item, "custom_id", None) == "ticket_btn_transcript")
        self.assertEqual((saving.label, saving.disabled), ("Saving transcript…", True))
        # every close archives, and the archive updates the card it just posted
        archive.assert_awaited_once_with(
            channel, ticket, closed_by_id=STAFF_ID, card=channel.send.return_value, resolved=None, dm=True, staff_solved=False
        )

    async def test_close_always_archives_swaps_in_the_link_and_dms_the_owner(self):
        owner = SimpleNamespace(id=OWNER_ID, roles=[], send=AsyncMock())
        cog, channel = make_cog(), make_channel(owner)
        page = StoredPage(token="e" * 32, expires_at=None)
        with (
            patch.object(cog_module, "mark_closed", AsyncMock(return_value=make_ticket(status="closed"))),
            patch.object(cog, "_archive", AsyncMock(return_value=True)) as archive,
            patch.object(cog_module, "get_page_for_channel", AsyncMock(return_value=page)),
            patch.object(cog, "_set_row", AsyncMock()) as set_row,
        ):
            await cog.close_ticket(channel, closer_id=OWNER_ID, reason=None)
        archive.assert_awaited_once()
        card, row = set_row.await_args.args
        self.assertIs(card, channel.send.return_value)
        link = row.children[0]
        self.assertEqual(link.url, f"{SETTINGS.ticket_transcript_public_url}/t/{'e' * 32}")
        self.assertEqual(link.label, "Transcript")
        owner.send.assert_awaited_once()

    async def test_failed_archive_leaves_a_retry_button(self):
        cog, channel = make_cog(), make_channel()
        with (
            patch.object(cog, "_archive", AsyncMock(return_value=False)),
            patch.object(cog, "_set_row", AsyncMock()) as set_row,
        ):
            self.assertIsNone(await cog.archive(channel, make_ticket(), closed_by_id=STAFF_ID, card="closed card"))
        card, row = set_row.await_args.args
        retry = row.children[0]
        self.assertEqual((card, retry.custom_id, retry.label, retry.disabled), ("closed card", views.TRANSCRIPT_BUTTON_ID, "Retry transcript", False))

    async def test_closing_twice_does_nothing(self):
        cog, channel = make_cog(), make_channel()
        with patch.object(cog_module, "mark_closed", AsyncMock(return_value=None)):
            self.assertIsNone(await cog.close_ticket(channel, closer_id=STAFF_ID, reason=None))
        channel.set_permissions.assert_not_called()
        channel.edit.assert_not_called()

    async def test_reopen_restores_owner_name_and_close_button(self):
        cog, owner, channel = make_cog(), Snow(OWNER_ID), make_channel()
        channel.guild.get_member = lambda _id: owner
        with (
            patch.object(cog_module, "mark_reopened", AsyncMock(return_value=make_ticket())),
            patch.object(cog, "_set_row", AsyncMock()) as set_row,
        ):
            await cog.reopen_ticket(channel, opener_id=STAFF_ID)
        edits = [call.kwargs for call in channel.edit.await_args_list]
        self.assertIs(edits[0]["overwrites"][owner].send_messages, True)
        self.assertEqual(edits[1], {"name": "crash-0042"})
        self.assertEqual(custom_ids(set_row.await_args.args[1]), [views.AI_BUTTON_ID, "ticket_btn_close", views.SOLVED_BUTTON_ID])

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
        with (
            patch.object(cog_module, "mark_reopened", AsyncMock(return_value=make_ticket())),
            patch.object(cog, "_set_row", AsyncMock()),
        ):
            await cog.reopen_ticket(channel, opener_id=STAFF_ID)
        ai_cog.forget_archived.assert_called_once_with(900)

    async def test_archive_dms_the_owner_a_card_with_a_link_button(self):
        owner = SimpleNamespace(id=OWNER_ID, roles=[], send=AsyncMock())
        cog, channel = make_cog(), make_channel(owner)
        page = StoredPage(token="a" * 32, expires_at=datetime(2026, 11, 2, tzinfo=timezone.utc))
        with (
            patch.object(cog, "_archive", AsyncMock(return_value=True)),
            patch.object(cog_module, "get_page_for_channel", AsyncMock(return_value=page)),
        ):
            self.assertIs(await cog.archive(channel, make_ticket(), closed_by_id=STAFF_ID), page)
        view = owner.send.await_args.kwargs["view"]
        self.assertIn("<t:", texts(view))
        button = next(item for item in view.walk_children() if isinstance(item, discord.ui.Button))
        self.assertEqual(button.url, f"{SETTINGS.ticket_transcript_public_url}/t/{'a' * 32}")

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

    async def test_build_page_hosts_the_export_at_the_channels_existing_address(self):
        cog, channel = make_cog(), make_channel()
        existing, page = StoredPage(token="a" * 32, expires_at=None), StoredPage(token="b" * 32, expires_at=None)
        with (
            patch.object(cog, "export_html", AsyncMock(return_value=b"<html/>")),
            patch.object(cog_module, "get_page_for_channel", AsyncMock(return_value=existing)) as current,
            patch.object(cog_module, "save_page", AsyncMock(return_value=page)) as save,
        ):
            self.assertIs(await cog.build_page(channel), page)
        current.assert_awaited_once_with(900)
        save.assert_awaited_once_with(SETTINGS, b"<html/>", reuse=existing)
        with patch.object(cog, "export_html", AsyncMock(return_value=None)):
            self.assertIsNone(await cog.build_page(channel))

    def delete_patches(self, current, scheduled=None):
        """delete_ticket's DB calls; current: the ticket as re-read after the notice."""
        scheduled = scheduled or make_ticket(status="closed", language="es")
        return (
            patch.object(cog_module, "schedule_delete", AsyncMock(return_value=scheduled)),
            patch.object(cog_module.asyncio, "sleep", AsyncMock()),
            patch.object(cog_module, "get_ticket_by_channel", AsyncMock(return_value=current)),
            patch.object(cog_module, "mark_deleted", AsyncMock()),
        )

    async def test_delete_posts_a_notice_persists_the_deadline_waits_then_deletes(self):
        cog, channel = make_cog(), make_channel()
        due = make_ticket(status="closed", delete_at=datetime.now(timezone.utc))
        schedule_p, sleep_p, current_p, deleted_p = self.delete_patches(due)
        events = []
        channel.send = AsyncMock(side_effect=lambda **_: events.append("notice"))
        on_notice = AsyncMock(side_effect=lambda: events.append("on_notice"))
        before = datetime.now(timezone.utc)
        with (
            patch.object(cog, "archive", AsyncMock()) as archive,
            schedule_p as schedule,
            sleep_p as sleep,
            current_p,
            deleted_p as deleted,
        ):
            sleep.side_effect = lambda _s: events.append("wait")
            channel.delete.side_effect = lambda **_: events.append("delete")
            self.assertTrue(await cog.delete_ticket(channel, make_ticket(status="closed"), on_notice=on_notice))
        self.assertEqual(events, ["notice", "on_notice", "wait", "delete"])
        channel_id, delete_at = schedule.await_args.args
        self.assertEqual(channel_id, 900)
        self.assertAlmostEqual((delete_at - before).total_seconds(), cog_module.DELETE_NOTICE_SECONDS, delta=5)
        sleep.assert_awaited_once_with(cog_module.DELETE_NOTICE_SECONDS)
        # The notice is in the ticket's support language, with the deadline as a Discord timestamp.
        self.assertEqual(
            texts(channel.send.call_args.kwargs["view"]), f"🗑️ Este ticket se eliminará <t:{int(delete_at.timestamp())}:R>."
        )
        self.assertEqual(
            channel.send.call_args.kwargs["allowed_mentions"].to_dict(), discord.AllowedMentions.none().to_dict()
        )
        archive.assert_not_called()
        deleted.assert_awaited_once_with(900)
        self.assertEqual(cog._deleting, set())

    async def test_a_reopen_during_the_notice_wins(self):
        cog, channel = make_cog(), make_channel()
        for current in (make_ticket(status="open"), make_ticket(status="closed", delete_at=None), None):
            schedule_p, sleep_p, current_p, deleted_p = self.delete_patches(current)
            with schedule_p, sleep_p, current_p, deleted_p as deleted:
                self.assertTrue(await cog.delete_ticket(channel, make_ticket(status="closed")))
            deleted.assert_not_called()
        channel.delete.assert_not_called()
        self.assertEqual(cog._deleting, set())

    async def test_a_ticket_no_longer_closed_gets_no_notice(self):
        cog, channel = make_cog(), make_channel()
        with (
            patch.object(cog_module, "schedule_delete", AsyncMock(return_value=None)),
            patch.object(cog_module.asyncio, "sleep", AsyncMock()) as sleep,
        ):
            self.assertTrue(await cog.delete_ticket(channel, make_ticket(status="closed")))
        channel.send.assert_not_called()
        sleep.assert_not_called()
        channel.delete.assert_not_called()

    async def test_deleting_a_gone_channel_is_quiet_but_a_refusal_is_a_failure(self):
        cog, channel = make_cog(), make_channel()
        response = SimpleNamespace(status=404, reason="gone")
        channel.delete = AsyncMock(side_effect=discord.NotFound(response, "Unknown Channel"))
        due = make_ticket(status="closed", delete_at=datetime.now(timezone.utc))
        with self.assertNoLogs(cog_module.log, "ERROR"):
            schedule_p, sleep_p, current_p, deleted_p = self.delete_patches(due)
            with schedule_p, sleep_p, current_p, deleted_p:
                self.assertTrue(await cog.delete_ticket(channel, make_ticket(status="closed")))
        channel.delete = AsyncMock(side_effect=discord.Forbidden(SimpleNamespace(status=403, reason="no"), "Missing Permissions"))
        with self.assertLogs(cog_module.log, "ERROR"):
            schedule_p, sleep_p, current_p, deleted_p = self.delete_patches(due)
            with schedule_p, sleep_p, current_p, deleted_p:
                self.assertFalse(await cog.delete_ticket(channel, make_ticket(status="closed")))
        self.assertEqual(cog._deleting, set())

    async def test_second_delete_is_ignored(self):
        cog, channel = make_cog(), make_channel()
        cog._deleting.add(900)
        with (
            patch.object(cog_module, "schedule_delete", AsyncMock()) as schedule,
            patch.object(cog_module, "mark_deleted", AsyncMock()) as deleted,
        ):
            self.assertTrue(await cog.delete_ticket(channel, make_ticket()))
        schedule.assert_not_called()
        deleted.assert_not_called()
        channel.send.assert_not_called()

    async def test_schedule_delete_sql_only_touches_closed_tickets(self):
        pool = SimpleNamespace(fetchrow=AsyncMock(return_value=None))
        when = datetime(2099, 1, 1, tzinfo=timezone.utc)
        self.assertIsNone(await db.schedule_delete(900, when, pool=pool))
        query, *params = pool.fetchrow.await_args.args
        self.assertIn("SET delete_at = $2", query)
        self.assertIn("status = 'closed'", query)
        self.assertEqual(params, [900, when])


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


BOT_ID = 9  # make_cog's bot user


def at(minute):
    return datetime(2026, 10, 1, 12, minute, tzinfo=timezone.utc)


def said(message_id, author_id, minute, content="", reference=None):
    """Enough of a discord.Message for hide_closed_messages and chat_exporter."""
    author = SimpleNamespace(
        id=author_id, name=f"user{author_id}", display_name=f"User {author_id}", discriminator="0", bot=author_id == BOT_ID,
        public_flags=SimpleNamespace(verified_bot=False), display_avatar="https://cdn/a.png", created_at=at(0), joined_at=at(0),
    )
    return SimpleNamespace(
        id=message_id, author=author, content=content, created_at=at(minute), edited_at=None, type=discord.MessageType.default,
        reference=reference, interaction=None, interaction_metadata=None, stickers=[], attachments=[], embeds=[],
        components=[], reactions=[], webhook_id=None, mentions=[], channel=SimpleNamespace(type=discord.ChannelType.text),
    )


class ClosedStretchTests(unittest.IsolatedAsyncioTestCase):
    """Messages sent while a ticket was closed never reach a transcript; a notice stands in for each closed stretch."""

    SPANS = [(at(2), at(5)), (at(7), None)]

    def test_spans_are_past_periods_plus_the_current_close(self):
        past = ((at(2), at(5)),)
        self.assertEqual(db.closed_spans(make_ticket(closed_periods=past)), [(at(2), at(5))])
        self.assertEqual(db.closed_spans(make_ticket(status="closed", closed_at=at(7), closed_periods=past)), self.SPANS)
        self.assertEqual(db.closed_spans(None), [])  # no tickets row: nothing is hidden

    def test_rows_turn_ranges_into_pairs_and_drop_unbounded_ones(self):
        from asyncpg import Range

        values = [42, 1, OWNER_ID, 900, "bug", "open", None, "en", None, None, None, None, at(0), None]
        row = dict(zip(db.Ticket.__slots__, [*values, [Range(at(2), at(5)), Range(None, at(6)), Range(empty=True)], None]))
        self.assertEqual(db._ticket(row).closed_periods, ((at(2), at(5)),))

    def test_closed_messages_collapse_into_one_count_per_stretch_and_bot_cards_stay(self):
        messages = [
            said(1, BOT_ID, 0), said(2, OWNER_ID, 1), said(3, BOT_ID, 2), said(4, STAFF_ID, 2), said(5, STAFF_ID, 4),
            said(6, BOT_ID, 5), said(7, STAFF_ID, 5), said(8, BOT_ID, 7), said(9, STAFF_ID, 8),
        ]
        entries = db.hide_closed_messages(messages, self.SPANS, bot_id=BOT_ID)
        self.assertEqual(
            [entry.notice if isinstance(entry, db.ClosedGap) else entry.id for entry in entries],
            [
                1, 2, 3, "2 messages were sent while this ticket was closed.", 6, 7, 8,
                "1 message was sent while this ticket was closed.",
            ],
        )
        self.assertEqual(entries[3].created_at, at(2))  # where the first hidden message was
        self.assertEqual(db.hide_closed_messages(messages, [], bot_id=BOT_ID), messages)

    async def test_html_export_drops_closed_messages_and_marks_each_gap(self):
        cog, channel = make_cog(), make_channel()
        channel.topic, channel.created_at = "Ticket #0042 · bug · owner 100", at(0)
        quoting_hidden = SimpleNamespace(message_id=discord.utils.time_snowflake(at(3)), channel_id=900)
        oldest_first = [
            said(1, BOT_ID, 0, "welcome card"), said(2, OWNER_ID, 1, "it crashes"), said(3, BOT_ID, 2, "closed card"),
            said(4, STAFF_ID, 3, "zzhidden one"), said(5, STAFF_ID, 4, "zzhidden two"), said(6, BOT_ID, 5, "reopened card"),
            said(7, STAFF_ID, 6, "following up", reference=quoting_hidden), said(8, BOT_ID, 7, "closed again"),
            said(9, STAFF_ID, 8, "zzhidden three"),
        ]

        async def history(limit):
            for message in reversed(oldest_first):
                yield message

        channel.history = history
        channel.fetch_message = AsyncMock(side_effect=AssertionError("fetched a hidden message"))
        ticket = make_ticket(status="closed", closed_at=at(7), closed_periods=((at(2), at(5)),))
        with patch.object(cog_module, "get_ticket_by_channel", AsyncMock(return_value=ticket)):
            html = (await cog.export_html(channel)).decode()

        self.assertNotIn("zzhidden", html)
        chat = html[html.index('<div class="chatlog">') :]
        markers = ("closed card", "2 messages were sent", "reopened card", "following up", "closed again", "1 message was sent")
        spots = [chat.index(marker) for marker in markers]
        self.assertEqual(spots, sorted(spots))
        self.assertLess(spots[-1], chat.index('<div class="footer">'))
        self.assertIn("6 messages", html.split("<style>")[0])  # the link preview counts only what's shown


class TicketReplyTrackingTests(unittest.IsolatedAsyncioTestCase):
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
        with patch.object(cog_module, "note_ticket_message", AsyncMock()) as self.noted:
            await cog.on_message(message)

    def test_pings_staff_counts_typed_staff_mentions_only(self):
        # Moved to utils.permissions for automod's server-wide staff_ping filter.
        from bulmaai.utils.permissions import has_staff_rank, pings_staff

        for kwargs in ({"users": [STAFF_ID]}, {"roles": [MOD_ROLE]}):
            self.assertTrue(pings_staff(self.message(member(OWNER_ID), **kwargs), SETTINGS))
        for kwargs in ({"users": [555]}, {"roles": [TESTER_ROLE]}, {}):
            self.assertFalse(pings_staff(self.message(member(OWNER_ID), **kwargs), SETTINGS))
        self.assertTrue(has_staff_rank(member(STAFF_ID, HELPER_ROLE), SETTINGS))
        self.assertFalse(has_staff_rank(member(OWNER_ID), SETTINGS))

    async def test_staff_pings_in_a_ticket_no_longer_warn_from_here(self):
        message = self.message(member(OWNER_ID), users=[STAFF_ID])
        await self.run_listener(message)
        message.reply.assert_not_called()  # automod's staff_ping filter owns this now

    async def test_every_human_message_in_a_ticket_feeds_the_reply_countdown(self):
        for author in (member(OWNER_ID), member(STAFF_ID, HELPER_ROLE)):
            await self.run_listener(self.message(author))
            self.noted.assert_awaited_once_with(900, author.id)
        bot_message = self.message(member(9))
        bot_message.author.bot = True
        await self.run_listener(bot_message)
        self.noted.assert_not_called()
        elsewhere = self.message(member(OWNER_ID))
        elsewhere.channel = SimpleNamespace(id=901, category_id=99)
        await self.run_listener(elsewhere)
        self.noted.assert_not_called()  # no DB call outside the ticket category


class InteractionGateTests(unittest.IsolatedAsyncioTestCase):
    def interaction(self, user, custom_id=None, card=None):
        """A click on custom_id on the closed card (or card)."""
        return SimpleNamespace(
            user=user,
            channel_id=900,
            data={"custom_id": custom_id},
            message=sent_message(card or views.closed_view(None, None)),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(), edit_message=AsyncMock()),
            edit_original_response=AsyncMock(),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    def busy_row(self, interaction):
        view = interaction.response.edit_message.await_args.kwargs["view"]
        return {item.custom_id or item.url: (item.label, item.disabled) for item in walk(view) if isinstance(item, discord.ui.Button)}

    async def test_non_staff_cannot_use_any_button(self):
        cog = make_cog()
        outsider = member(STAFF_ID)
        with patch.object(cog_module, "get_ticket_by_channel", AsyncMock(return_value=make_ticket())), \
                patch.object(cog, "archive", AsyncMock()) as archive, \
                patch.object(cog, "close_ticket", AsyncMock()) as close, \
                patch.object(cog, "reopen_ticket", AsyncMock()) as reopen, \
                patch.object(cog, "delete_ticket", AsyncMock()) as delete:
            for handler in (cog.on_transcript, cog.on_close, cog.on_reopen, cog.on_delete):
                interaction = self.interaction(outsider)
                await handler(interaction)
                interaction.response.send_message.assert_awaited_once()
                self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])
        for action in (archive, close, reopen, delete):
            action.assert_not_called()

    async def test_owner_can_close_but_not_reopen_or_transcript(self):
        cog = make_cog()
        owner = member(OWNER_ID)
        with patch.object(cog_module, "get_ticket_by_channel", AsyncMock(return_value=make_ticket())):
            interaction = self.interaction(owner)
            await cog.on_close(interaction)
            prompt = interaction.response.send_message.await_args.kwargs
            self.assertTrue(prompt["ephemeral"])
            self.assertIsInstance(prompt["view"], views.ClosePrompt)
            with (
                patch.object(cog, "close_ticket", AsyncMock(return_value=make_ticket(status="closed"))) as close,
                patch.object(cog_module, "get_page_for_channel", AsyncMock(return_value=StoredPage(token="d" * 32, expires_at=None))),
            ):
                interaction.channel = make_channel()
                interaction.edit_original_response = AsyncMock()
                await cog._close_from_prompt(interaction)
            close.assert_awaited_once_with(interaction.channel, closer_id=OWNER_ID, reason=None)
            self.assertIn("d" * 32, str(interaction.edit_original_response.await_args.kwargs["view"].to_components()))
        with patch.object(cog_module, "get_ticket_by_channel", AsyncMock(return_value=make_ticket(status="closed"))):
            for handler in (cog.on_reopen, cog.on_transcript):
                interaction = self.interaction(owner)
                await handler(interaction)
                interaction.response.send_message.assert_awaited_once()

    async def test_retry_or_old_transcript_button_rearchives_into_the_card_without_posting(self):
        cog = make_cog()
        ai_cog = SimpleNamespace(forget_archived=MagicMock(), _close_ticket=AsyncMock(return_value=True))
        cog.bot.get_cog = lambda name: ai_cog if name == "AITicketsCog" else None
        retry_card = views.closed_view(None, None)
        retry_card.remove_item(views.ACTIONS_ID)
        retry_card.add_item(views.closed_row())
        interaction = self.interaction(member(STAFF_ID, HELPER_ROLE), views.TRANSCRIPT_BUTTON_ID, retry_card)
        interaction.channel = make_channel()
        page = StoredPage(token="c" * 32, expires_at=None)
        with (
            patch.object(cog_module, "get_ticket_by_channel", AsyncMock(return_value=make_ticket(status="closed", closed_by=OWNER_ID))),
            patch.object(cog_module, "get_page_for_channel", AsyncMock(return_value=page)),
            patch.object(cog, "_set_row", AsyncMock()) as set_row,
        ):
            await cog.on_transcript(interaction)
        ai_cog.forget_archived.assert_called_once_with(900)  # the close already archived it once
        self.assertEqual(ai_cog._close_ticket.await_args.kwargs["closed_by_id"], OWNER_ID)
        self.assertFalse(ai_cog._close_ticket.await_args.kwargs["staff_solved"])
        # first the card shows the click is being handled, with its row disabled
        self.assertEqual(
            self.busy_row(interaction),
            {
                views.TRANSCRIPT_BUTTON_ID: (v2.WORKING, True),
                views.REOPEN_BUTTON_ID: ("Re-Open", True),
                views.DELETE_BUTTON_ID: ("Delete", True),
            },
        )
        interaction.followup.send.assert_not_called()  # the card's link button is the only note
        interaction.edit_original_response.assert_not_called()  # nothing to restore
        message, row = set_row.await_args.args
        self.assertIs(message, interaction.message)
        self.assertEqual(row.children[0].url, f"{SETTINGS.ticket_transcript_public_url}/t/{'c' * 32}")
        self.assertFalse(row.get_item("ticket_btn_reopen").disabled)

    async def test_reopen_strips_the_closed_card_buttons(self):
        cog = make_cog()
        interaction = self.interaction(member(STAFF_ID, MOD_ROLE), views.REOPEN_BUTTON_ID)
        interaction.guild = SimpleNamespace(get_member=lambda _id: member(_id))
        interaction.channel = make_channel()
        with (
            patch.object(cog_module, "get_ticket_by_channel", AsyncMock(return_value=make_ticket(status="closed"))),
            patch.object(cog, "reopen_ticket", AsyncMock()) as reopen,
            patch.object(cog, "_set_row", AsyncMock()) as set_row,
        ):
            await cog.on_reopen(interaction)
        self.assertEqual(self.busy_row(interaction)[views.REOPEN_BUTTON_ID], (v2.WORKING, True))
        set_row.assert_awaited_once_with(interaction.message, None)
        reopen.assert_awaited_once()

    async def test_second_click_while_working_is_turned_away(self):
        cog = make_cog()
        cog._working.add(1)  # the first click on this card is still running
        interaction = self.interaction(member(STAFF_ID, MOD_ROLE), views.REOPEN_BUTTON_ID)
        interaction.guild = SimpleNamespace(get_member=lambda _id: member(_id))
        with (
            patch.object(cog_module, "get_ticket_by_channel", AsyncMock(return_value=make_ticket(status="closed"))),
            patch.object(cog, "reopen_ticket", AsyncMock()) as reopen,
        ):
            await cog.on_reopen(interaction)
        interaction.response.send_message.assert_awaited_once_with(v2.ALREADY_WORKING, ephemeral=True)
        interaction.response.edit_message.assert_not_called()
        reopen.assert_not_called()
        self.assertEqual(cog._working, {1})  # still the first click's

    async def test_failed_work_restores_the_row_and_releases_the_card(self):
        cog = make_cog()
        interaction = self.interaction(member(STAFF_ID, MOD_ROLE), views.REOPEN_BUTTON_ID)
        interaction.guild = SimpleNamespace(get_member=lambda _id: member(_id))
        interaction.channel = make_channel()
        with (
            patch.object(cog_module, "get_ticket_by_channel", AsyncMock(return_value=make_ticket(status="closed"))),
            patch.object(cog, "reopen_ticket", AsyncMock(side_effect=RuntimeError("boom"))),
            self.assertLogs(v2.log, "ERROR"),
        ):
            await cog.on_reopen(interaction)
        restored = interaction.edit_original_response.await_args.kwargs["view"]
        reopen_button = next(item for item in walk(restored) if getattr(item, "custom_id", None) == views.REOPEN_BUTTON_ID)
        self.assertEqual((reopen_button.label, reopen_button.disabled), ("Re-Open", False))
        interaction.followup.send.assert_awaited_once_with(views.MSG["failed"], ephemeral=True)
        self.assertEqual(cog._working, set())

    async def test_helper_cannot_delete_but_mod_can(self):
        cog = make_cog()
        closed = make_ticket(status="closed")
        with patch.object(cog_module, "get_ticket_by_channel", AsyncMock(return_value=closed)), \
                patch.object(cog, "delete_ticket", AsyncMock(return_value=True)) as delete:
            interaction = self.interaction(member(STAFF_ID, HELPER_ROLE))
            await cog.on_delete(interaction)
            delete.assert_not_called()
            interaction = self.interaction(member(STAFF_ID, MOD_ROLE), views.DELETE_BUTTON_ID)
            interaction.channel = make_channel()
            await cog.on_delete(interaction)
            delete.assert_awaited_once()
        self.assertEqual(self.busy_row(interaction)[views.DELETE_BUTTON_ID], (v2.WORKING, True))
        interaction.followup.send.assert_not_called()  # delete_ticket's notice in the channel is the only message

    async def test_delete_click_frees_the_card_once_the_notice_is_up(self):
        """So a Re-Open on the same closed card within the notice still goes through."""
        cog = make_cog()
        interaction = self.interaction(member(STAFF_ID, MOD_ROLE), views.DELETE_BUTTON_ID)
        interaction.channel = make_channel()

        async def delete_ticket(channel, ticket, *, on_notice):
            self.assertIn(interaction.message.id, cog._working)
            await on_notice()
            self.assertNotIn(interaction.message.id, cog._working)
            restored = interaction.edit_original_response.await_args.kwargs["view"]
            buttons = {item.custom_id: item.disabled for item in walk(restored) if getattr(item, "custom_id", None)}
            self.assertEqual(buttons[views.REOPEN_BUTTON_ID], False)
            return True

        with patch.object(cog_module, "get_ticket_by_channel", AsyncMock(return_value=make_ticket(status="closed"))), \
                patch.object(cog, "delete_ticket", AsyncMock(side_effect=delete_ticket)) as delete:
            await cog.on_delete(interaction)
        delete.assert_awaited_once()
        interaction.followup.send.assert_not_called()
        self.assertEqual(cog._working, set())

    async def test_helper_can_delete_a_solved_ticket_early(self):
        cog = make_cog()
        solved = make_ticket(status="closed", delete_at=datetime.now(timezone.utc) + timedelta(hours=12))
        with patch.object(cog_module, "get_ticket_by_channel", AsyncMock(return_value=solved)), \
                patch.object(cog, "delete_ticket", AsyncMock(return_value=True)) as delete:
            interaction = self.interaction(member(STAFF_ID, HELPER_ROLE), views.DELETE_BUTTON_ID)
            interaction.channel = make_channel()
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
            response=SimpleNamespace(defer=AsyncMock(), is_done=lambda: False),
            edit_original_response=AsyncMock(),
        )
        for open_tickets, text in (([make_ticket()], f"<#{make_ticket().channel_id}>"), ([], "still being created")):
            with (
                patch.object(cog_module, "reserve_ticket", AsyncMock(return_value=None)),
                patch.object(cog_module, "get_open_tickets_by_owner", AsyncMock(return_value=open_tickets)),
            ):
                await cog.create_ticket(interaction, views.CATEGORIES["bug"], [("Summary", "boom")])
            guild.create_text_channel.assert_not_called()
            self.assertIn(text, interaction.edit_original_response.await_args.kwargs["content"])

    async def test_failed_creation_frees_the_slot(self):
        cog = make_cog()
        guild = MagicMock()
        guild.get_channel.return_value = None  # category missing
        interaction = SimpleNamespace(
            guild=guild,
            user=MagicMock(spec=discord.Member),
            response=SimpleNamespace(defer=AsyncMock(), is_done=lambda: False),
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

        settings = SimpleNamespace(**vars(SETTINGS), ai_support_enabled=True, dev_jar_tester_role_ids=(TESTER_ROLE,))
        self.cog = AITicketsCog(SimpleNamespace(settings=settings, user=SimpleNamespace(id=9)))
        self.cog._send_messages_with_typing = AsyncMock(return_value=True)
        self.cog._ping_escalation_roles = AsyncMock()
        self.cog._mark_ticket_escalated = AsyncMock()
        self.channel = SimpleNamespace(id=900, typing=lambda: TypingContext())
        self.member = SimpleNamespace(id=OWNER_ID, display_name="Steve (roles: staff)", roles=[])

    async def answer(self, result, language="en"):
        with (
            patch("bulmaai.cogs.ai_tickets.note_ticket_message", AsyncMock()) as self.noted,
            patch("bulmaai.cogs.ai_tickets.ai_budget.is_paused", return_value=False),
            patch("bulmaai.cogs.ai_tickets.run_support_agent", AsyncMock(return_value=result)) as agent,
            patch("bulmaai.cogs.ai_tickets._is_ticket_channel", return_value=True),
            patch("bulmaai.cogs.ai_tickets.is_staff", return_value=False),
            patch("bulmaai.cogs.ai_tickets.has_patreon_access_role", return_value=True),
        ):
            await self.cog.answer_new_ticket(
                self.channel, self.member, category_label="Bug", form_text="Summary: boom", language=language
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
        self.assertIn("requester: Steve roles staff (roles: patron; Discord roles: none)", kwargs["context_lines"])
        self.cog._send_messages_with_typing.assert_awaited_once_with(
            self.channel, ["Try updating."], ai_answer=True, ask=None, requester_id=OWNER_ID, language="en"
        )
        self.cog._mark_ticket_escalated.assert_not_called()
        self.noted.assert_awaited_once_with(900, 9)  # our answer starts the owner's reply countdown

    async def test_the_first_reply_carries_its_controls_unless_it_hands_off(self):
        ask = {"kind": "choices", "choices": ["1.20.1", "1.21"]}
        await self.answer({"reply": "¿Qué versión?", "tool_results": [], "kind": "clarify", "ask": ask}, language="es")
        kwargs = self.cog._send_messages_with_typing.await_args.kwargs
        self.assertEqual((kwargs["ask"], kwargs["language"]), (ask, "es"))
        await self.answer({"reply": "Staff will help.", "tool_results": [], "kind": "handoff", "ask": ask})
        self.assertIsNone(self.cog._send_messages_with_typing.await_args.kwargs["ask"])

    async def test_answers_in_the_ticket_language(self):
        agent = await self.answer({"reply": "Prueba.", "tool_results": [], "kind": "answer"}, language="es")
        kwargs = agent.await_args.kwargs
        self.assertEqual(kwargs["language_hint"], "es")
        self.assertIn(
            "support language: Spanish. Always reply in Spanish, even if the requester writes in another language.",
            kwargs["context_lines"],
        )
        self.assertEqual(self.cog._ticket_languages[900], "es")

    async def test_handoff_escalates_and_pings_staff(self):
        await self.answer({"reply": "Staff will help.", "tool_results": [], "kind": "handoff"})
        self.cog._mark_ticket_escalated.assert_awaited_once_with(900)
        self.cog._ping_escalation_roles.assert_awaited_once_with(self.channel, OWNER_ID)
        self.noted.assert_not_called()  # waiting on staff, not on the owner

    async def test_silent_when_there_is_nothing_to_say(self):
        await self.answer({"reply": "(no reply)", "tool_results": [], "kind": None})
        self.cog._send_messages_with_typing.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class AiToggleTests(unittest.IsolatedAsyncioTestCase):
    async def test_button_shows_state_and_toggle_flips_it_publicly(self):
        on, off = views.control_row(True), views.control_row(False)
        self.assertEqual((on.children[0].label, off.children[0].label), ("AI support: On", "AI support: Off"))
        cog = make_cog()
        ai_cog = SimpleNamespace(is_ticket_ai_enabled=lambda _id: True, set_ticket_ai_enabled=AsyncMock())
        cog.bot.get_cog = lambda name: ai_cog if name == "AITicketsCog" else None
        welcome = views.welcome_view(number=42, category=views.CATEGORIES["bug"], answers=[], mention="<@1>", language="en")
        interaction = SimpleNamespace(
            channel_id=900,
            user=SimpleNamespace(id=STAFF_ID, roles=[SimpleNamespace(id=HELPER_ROLE)], mention=f"<@{STAFF_ID}>"),
            data={"custom_id": views.AI_BUTTON_ID},
            message=sent_message(welcome),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), edit_message=AsyncMock()),
            edit_original_response=AsyncMock(),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with patch.object(cog_module, "get_ticket_by_channel", AsyncMock(return_value=make_ticket())):
            await cog.on_toggle_ai(interaction)
        ai_cog.set_ticket_ai_enabled.assert_awaited_once_with(900, False)
        self.assertIn("AI support is now **off**", interaction.followup.send.await_args.args[0])
        busy = interaction.response.edit_message.await_args.kwargs["view"]
        labels = [(item.label, item.disabled) for item in walk(busy) if isinstance(item, discord.ui.Button)]
        self.assertEqual(labels, [(v2.WORKING, True), ("Close | Cerrar", True), ("Solved", True)])


class BusyCardTests(unittest.IsolatedAsyncioTestCase):
    async def test_busy_disables_the_clicked_row_and_relabels_only_the_clicked_button(self):
        card = views.closed_view(None, None)
        card.remove_item(views.ACTIONS_ID)
        card.add_item(views.closed_row("https://tickets.example/t/x"))
        view = v2.busy(discord.ui.DesignerView.from_message(sent_message(card), timeout=None), views.DELETE_BUTTON_ID)
        link, reopen, delete = [item for item in walk(view) if isinstance(item, discord.ui.Button)]
        self.assertEqual((link.url, link.label, link.disabled), ("https://tickets.example/t/x", "Transcript", False))
        self.assertEqual((reopen.label, reopen.disabled, str(reopen.emoji)), ("Re-Open", True, "🔓"))
        self.assertEqual((delete.label, delete.disabled, delete.emoji), (v2.WORKING, True, None))
        self.assertEqual(delete.style, discord.ButtonStyle.danger)  # keeps its colour

    async def test_busy_leaves_other_rows_alone(self):
        prompt = views.ClosePrompt(AsyncMock())
        other = discord.ui.ActionRow(discord.ui.Button(label="Other", custom_id="other"))
        prompt.add_item(other)
        v2.busy(prompt, prompt._close_id)
        self.assertEqual((other.children[0].label, other.children[0].disabled), ("Other", False))
        close, cancel = [item for item in walk(prompt.children[0]) if isinstance(item, discord.ui.Button)]
        self.assertEqual((close.label, close.disabled, cancel.disabled), (v2.WORKING, True, True))

    async def test_close_prompt_confirm_shows_working_and_ignores_a_second_click(self):
        on_confirm = AsyncMock()
        prompt = views.ClosePrompt(on_confirm)
        first = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()))
        await prompt._confirm(first)
        on_confirm.assert_awaited_once_with(first)
        busy = first.response.edit_message.await_args.kwargs["view"]
        self.assertIs(busy, prompt)
        self.assertEqual(busy.children[0].items[1].children[0].label, v2.WORKING)
        second = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()))
        await prompt._confirm(second)
        second.response.send_message.assert_awaited_once_with(v2.ALREADY_WORKING, ephemeral=True)
        self.assertEqual(on_confirm.await_count, 1)


def click(user, custom_id, card, channel_id=900):
    return SimpleNamespace(
        type=discord.InteractionType.component,
        user=user,
        channel_id=channel_id,
        data={"custom_id": custom_id},
        message=sent_message(card),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(), edit_message=AsyncMock(), is_done=lambda: True),
        edit_original_response=AsyncMock(),
        followup=SimpleNamespace(send=AsyncMock()),
    )


class SolvedTests(unittest.IsolatedAsyncioTestCase):
    """Every solved close (staff Solved, or the owner confirming it to the AI): closed, the owner gets the solved DM,
    and the sweep deletes it SOLVED_DELETE_AFTER later unless staff re-open it."""

    URL = "https://tickets.example/t/x"
    LATER = datetime(2099, 1, 1, tzinfo=timezone.utc)
    REOPEN_ID = f"{views.SOLVED_REOPEN_PREFIX}42"

    def welcome(self):
        return views.welcome_view(number=42, category=views.CATEGORIES["bug"], answers=[], mention="<@1>", language="en")

    def solved_dm(self):
        return views.dm_solved(42, SimpleNamespace(icon=None), self.URL, None, self.LATER, "en")

    def test_control_row_has_the_solved_button(self):
        solved = views.control_row().children[-1]
        self.assertEqual((solved.custom_id, solved.label, solved.style), (views.SOLVED_BUTTON_ID, "Solved", discord.ButtonStyle.primary))

    def test_solved_window_is_one_hour_and_the_sweep_runs_every_minute(self):
        self.assertEqual(cog_module.SOLVED_DELETE_AFTER, timedelta(hours=1))
        self.assertEqual(cog_module.DELETE_NOTICE_SECONDS, 10)
        self.assertEqual((TicketsCog._delete_solved.hours, TicketsCog._delete_solved.minutes), (0, 1))

    async def test_solved_dm_links_the_transcript_and_says_when_it_is_deleted_without_a_reopen_button(self):
        guild, expires = SimpleNamespace(icon=None), datetime(2026, 11, 2, tzinfo=timezone.utc)
        view = views.dm_solved(42, guild, self.URL, expires, self.LATER, "en")
        text = texts(view)
        self.assertIn(f"<t:{int(expires.timestamp())}:f> (<t:{int(expires.timestamp())}:R>)", text)
        self.assertIn(f"The ticket will be deleted <t:{int(self.LATER.timestamp())}:R>.", text)
        self.assertNotIn("re-open", text.lower())
        self.assertEqual([item.url for item in walk(view) if isinstance(item, discord.ui.Button)], [self.URL])
        self.assertEqual(custom_ids(view), [])
        self.assertIn("kept permanently", texts(self.solved_dm()))

        no_page = views.dm_solved(42, guild, None, None, self.LATER, "en")
        self.assertNotIn("transcript", texts(no_page))
        self.assertIn("will be deleted", texts(no_page))
        self.assertEqual([item for item in walk(no_page) if isinstance(item, discord.ui.Button)], [])

    async def test_owner_cannot_mark_solved(self):
        cog = make_cog()
        interaction = click(member(OWNER_ID), views.SOLVED_BUTTON_ID, self.welcome())
        with (
            patch.object(cog_module, "get_ticket_by_channel", AsyncMock(return_value=make_ticket())),
            patch.object(cog, "close_ticket", AsyncMock()) as close,
        ):
            await cog.on_solved(interaction)
        interaction.response.send_message.assert_awaited_once_with(views.MSG["staff_only"], ephemeral=True)
        close.assert_not_called()

    async def test_solved_closes_resolved_with_a_deadline_and_sends_only_the_solved_dm(self):
        owner = SimpleNamespace(id=OWNER_ID, roles=[], send=AsyncMock())
        cog, channel = make_cog(), make_channel(owner)
        interaction = click(member(STAFF_ID, HELPER_ROLE), views.SOLVED_BUTTON_ID, self.welcome())
        interaction.channel = channel
        page = StoredPage(token="f" * 32, expires_at=None)
        before = datetime.now(timezone.utc)
        with (
            patch.object(cog_module, "get_ticket_by_channel", AsyncMock(return_value=make_ticket())),
            patch.object(cog_module, "mark_closed", AsyncMock(return_value=make_ticket(status="closed"))) as closed,
            patch.object(cog, "_archive", AsyncMock(return_value=True)) as archive,
            patch.object(cog_module, "get_page_for_channel", AsyncMock(return_value=page)),
            patch.object(cog, "_set_row", AsyncMock()),
        ):
            await cog.on_solved(interaction)
        delete_at = closed.await_args.kwargs["delete_at"]
        self.assertAlmostEqual((delete_at - before).total_seconds(), 3600, delta=60)
        reason = closed.await_args.kwargs["reason"]
        self.assertEqual(reason, f"✅ Solved. Deletes <t:{int(delete_at.timestamp())}:R> unless staff re-open it.")
        self.assertIs(archive.await_args.args[3], True)  # resolved
        self.assertIs(archive.await_args.args[4], True)  # staff_solved: may suggest support info
        self.assertIn("Solved. Deletes", texts(channel.send.await_args_list[0].kwargs["view"]))
        owner.send.assert_awaited_once()  # the solved DM only, no generic transcript DM
        dm = owner.send.await_args.kwargs["view"]
        self.assertIn("marked as solved", texts(dm))
        self.assertEqual(custom_ids(dm), [])
        interaction.followup.send.assert_not_called()

    async def test_owner_solved_closes_the_same_way_but_suggests_nothing(self):
        """AITicketsCog's owner paths (Yes on the prompt, or saying so) call solve_ticket without staff_solved."""
        owner = SimpleNamespace(id=OWNER_ID, roles=[], send=AsyncMock())
        cog, channel = make_cog(), make_channel(owner)
        before = datetime.now(timezone.utc)
        with (
            patch.object(cog_module, "mark_closed", AsyncMock(return_value=make_ticket(status="closed"))) as closed,
            patch.object(cog, "_archive", AsyncMock(return_value=True)) as archive,
            patch.object(cog_module, "get_page_for_channel", AsyncMock(return_value=None)),
            patch.object(cog, "_set_row", AsyncMock()),
        ):
            ticket = await cog.solve_ticket(channel, closer_id=OWNER_ID)
        self.assertEqual(ticket.status, "closed")
        kwargs = closed.await_args.kwargs
        self.assertEqual(kwargs["closed_by"], OWNER_ID)
        self.assertAlmostEqual((kwargs["delete_at"] - before).total_seconds(), 3600, delta=60)
        self.assertEqual(archive.await_args.args[2:], (OWNER_ID, True, False))  # closer, resolved, not staff_solved
        owner.send.assert_awaited_once()  # the solved DM, not the transcript one
        self.assertIn("marked as solved", texts(owner.send.await_args.kwargs["view"]))
        channel.delete.assert_not_called()

    async def test_solving_a_ticket_that_is_not_open_does_nothing_else(self):
        owner = SimpleNamespace(id=OWNER_ID, roles=[], send=AsyncMock())
        cog, channel = make_cog(), make_channel(owner)
        with patch.object(cog_module, "mark_closed", AsyncMock(return_value=None)):
            self.assertIsNone(await cog.solve_ticket(channel, closer_id=OWNER_ID))
        owner.send.assert_not_called()

    async def test_undeliverable_dm_posts_a_notice_in_the_channel(self):
        response = SimpleNamespace(status=403, reason="Forbidden")
        blocked = SimpleNamespace(id=OWNER_ID, send=AsyncMock(side_effect=discord.HTTPException(response, "DMs closed")))
        for owner in (blocked, None):
            cog, channel = make_cog(), make_channel(owner)
            with patch.object(cog_module, "get_page_for_channel", AsyncMock(return_value=None)):
                await cog._dm_solved(channel, make_ticket(status="closed"), self.LATER)
            kwargs = channel.send.await_args.kwargs
            self.assertIn(f"couldn't DM <@{OWNER_ID}>", texts(kwargs["view"]))
            self.assertEqual(kwargs["allowed_mentions"].to_dict(), discord.AllowedMentions.none().to_dict())

    async def test_old_dm_reopen_button_says_only_staff_reopen_in_the_owners_language(self):
        """Solved DMs sent before owners lost re-open still carry the button."""
        for ticket, language in ((make_ticket(status="closed", delete_at=self.LATER, language="pt"), "pt"), (None, "en")):
            cog = make_cog()
            cog.bot.get_channel = lambda _id: make_channel(Snow(OWNER_ID))
            interaction = click(SimpleNamespace(id=OWNER_ID), self.REOPEN_ID, self.solved_dm(), channel_id=555)
            with (
                patch.object(cog_module, "get_ticket_by_id", AsyncMock(return_value=ticket)) as lookup,
                patch.object(cog, "reopen_ticket", AsyncMock()) as reopen,
                patch.object(cog_module, "mark_reopened", AsyncMock()) as reopened,
            ):
                await cog.on_interaction(interaction)
            lookup.assert_awaited_once_with(42)
            interaction.response.send_message.assert_awaited_once_with(
                views.t(language, "reopen_staff"), ephemeral=True
            )
            reopen.assert_not_called()
            reopened.assert_not_called()
            interaction.response.edit_message.assert_not_called()
        self.assertIn("Só a staff pode reabrir", views.t("pt", "reopen_staff"))

    async def test_sweep_deletes_due_tickets_and_marks_missing_channels_deleted(self):
        cog, channel = make_cog(), make_channel()
        cog.bot.get_channel = lambda _id: channel if _id == 900 else None
        here, gone = make_ticket(status="closed"), make_ticket(status="closed", channel_id=901)
        with (
            patch.object(cog_module, "list_due_solved", AsyncMock(return_value=[here, gone])),
            patch.object(cog, "delete_ticket", AsyncMock(return_value=True)) as delete,
            patch.object(cog_module, "mark_deleted", AsyncMock()) as deleted,
        ):
            await cog._delete_solved()
        delete.assert_awaited_once_with(channel, here)
        deleted.assert_awaited_once_with(901)

    async def test_on_interaction_routes_the_solved_buttons(self):
        cog = make_cog()
        for custom_id, name in ((self.REOPEN_ID, "on_solved_reopen"), (views.SOLVED_BUTTON_ID, "on_solved")):
            interaction = click(member(OWNER_ID), custom_id, self.solved_dm())
            with patch.object(cog, name, AsyncMock()) as handler:
                await cog.on_interaction(interaction)
            handler.assert_awaited_once_with(interaction)

    async def test_reopen_sql_clears_the_deadline_and_refuses_expired_tickets(self):
        pool = SimpleNamespace(fetchrow=AsyncMock(return_value=None))
        await db.mark_reopened(900, pool=pool)
        query = pool.fetchrow.await_args.args[0]
        self.assertIn("delete_at = NULL", query)
        self.assertIn("(delete_at IS NULL OR delete_at > now())", query)
        await db.mark_closed(900, closed_by=STAFF_ID, reason=None, delete_at=self.LATER, pool=pool)
        self.assertEqual(pool.fetchrow.await_args.args[-1], self.LATER)


class SupportLanguageTests(unittest.IsolatedAsyncioTestCase):
    """Per-ticket support language: language roles at creation, the welcome select and the picker."""

    def test_role_languages_in_en_es_pt_order(self):
        self.assertEqual(role_languages(member(1, PT_ROLE, HELPER_ROLE, EN_ROLE), SETTINGS), ["en", "pt"])
        self.assertEqual(role_languages(member(1, HELPER_ROLE), SETTINGS), [])
        unset = SimpleNamespace(announcement_role_en_id=None, announcement_role_es_id=ES_ROLE, announcement_role_pt_id=None)
        self.assertEqual(role_languages(member(1, EN_ROLE, ES_ROLE), unset), ["es"])

    async def create(self, *role_ids):
        cog = make_cog()
        owner = MagicMock(spec=discord.Member)
        owner.id, owner.mention = OWNER_ID, f"<@{OWNER_ID}>"
        owner.roles = [SimpleNamespace(id=role_id) for role_id in role_ids]
        owner.send = AsyncMock()
        channel = make_channel()
        channel.mention, channel.jump_url = "<#900>", "https://discord.com/channels/1/900"
        channel.send = AsyncMock(return_value=SimpleNamespace(id=77))
        guild = MagicMock()
        guild.get_channel.return_value = MagicMock(spec=discord.CategoryChannel)
        guild.create_text_channel = AsyncMock(return_value=channel)
        interaction = SimpleNamespace(
            guild=guild,
            user=owner,
            response=SimpleNamespace(defer=AsyncMock(), is_done=lambda: False),
            edit_original_response=AsyncMock(),
        )
        ai_cog = SimpleNamespace(answer_new_ticket=AsyncMock())
        cog.bot.get_cog = lambda name: ai_cog if name == "AITicketsCog" else None
        intake = TicketIntake(channel_slug="crash", language="pt", summary="")
        with (
            patch.object(cog_module, "reserve_ticket", AsyncMock(return_value=make_ticket(channel_id=None, status="creating"))) as reserve,
            patch.object(cog_module, "attach_channel", AsyncMock()) as attach,
            patch.object(cog_module, "build_overwrites", return_value={}),
            patch.object(cog, "_triage", AsyncMock(return_value=intake)),
        ):
            await cog.create_ticket(interaction, views.CATEGORIES["bug"], [("Summary", "boom")])
        return SimpleNamespace(
            reserve=reserve.await_args.kwargs["language"],
            attach=attach.await_args.kwargs["language"],
            answer=ai_cog.answer_new_ticket.await_args.kwargs["language"],
            sends=channel.send.await_args_list,
            dm=texts(owner.send.await_args.kwargs["view"]),
            owner=owner,
        )

    async def test_one_language_role_sets_the_ticket_language(self):
        made = await self.create(ES_ROLE)
        self.assertEqual((made.reserve, made.attach, made.answer), ("es", "es", "es"))
        self.assertIn("¡Hola <@100>!", texts(made.sends[0].kwargs["view"]))
        self.assertIn("Tu ticket ha sido creado", made.dm)
        self.assertEqual(len(made.sends), 1)  # no picker

    async def test_without_language_roles_the_form_language_decides(self):
        made = await self.create()
        self.assertEqual((made.attach, made.answer), ("pt", "pt"))
        self.assertEqual(len(made.sends), 1)

    async def test_several_language_roles_get_a_picker_that_pings_the_owner(self):
        made = await self.create(PT_ROLE, EN_ROLE)
        self.assertEqual((made.attach, made.answer), ("pt", "pt"))  # the form's language until they pick
        picker = made.sends[1].kwargs
        self.assertEqual(custom_ids(picker["view"]), ["ticket_lang:en", "ticket_lang:pt"])
        self.assertIn("<@100>", texts(picker["view"]))
        self.assertEqual(picker["allowed_mentions"].users, [made.owner])

    def welcome_message(self):
        welcome = views.welcome_view(number=42, category=views.CATEGORIES["bug"], answers=[], mention="<@100>", language="en")
        return sent_message(welcome, message_id=77)

    async def change(self, user, custom_id, values=None, *, updated=None):
        cog, channel = make_cog(), make_channel()
        welcome = self.welcome_message()
        channel.fetch_message = AsyncMock(return_value=welcome)
        ai_cog = SimpleNamespace(forget_ticket_language=MagicMock())
        cog.bot.get_cog = lambda name: ai_cog if name == "AITicketsCog" else None
        card = views.language_picker("<@100>", ["en", "es"]) if custom_id != views.LANG_SELECT_ID else None
        interaction = click(user, custom_id, card) if card else SimpleNamespace(
            user=user,
            channel_id=900,
            data={"custom_id": custom_id},
            message=welcome,
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            edit_original_response=AsyncMock(),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        if values is not None:
            interaction.data["values"] = values
        interaction.user.mention = f"<@{user.id}>"
        interaction.channel = channel
        with (
            patch.object(cog_module, "get_ticket_by_channel", AsyncMock(return_value=make_ticket())),
            patch.object(cog_module, "set_language", AsyncMock(return_value=updated)) as set_language,
        ):
            await cog.on_language(interaction)
        return SimpleNamespace(
            interaction=interaction, channel=channel, welcome=welcome, set_language=set_language, ai_cog=ai_cog
        )

    async def test_owner_changes_the_language_from_the_welcome_select(self):
        done = await self.change(member(OWNER_ID), views.LANG_SELECT_ID, ["es"], updated=make_ticket(language="es"))
        done.set_language.assert_awaited_once_with(900, "es")
        done.ai_cog.forget_ticket_language.assert_called_once_with(900)
        edited = done.welcome.edit.await_args.kwargs["view"]
        self.assertIn("¡Hola <@100>!", texts(edited))
        self.assertEqual([o.value for o in edited.get_item(views.LANG_ROW_ID).children[0].options if o.default], ["es"])
        notice = done.channel.send.await_args.kwargs
        self.assertIn("🌐 Idioma de soporte cambiado a Español por <@100>.", texts(notice["view"]))
        self.assertEqual(notice["allowed_mentions"].to_dict(), discord.AllowedMentions.none().to_dict())

    async def test_staff_picks_from_the_picker_and_it_turns_into_a_confirmation(self):
        staff = SimpleNamespace(id=STAFF_ID, roles=[SimpleNamespace(id=HELPER_ROLE)])
        done = await self.change(staff, "ticket_lang:pt", updated=make_ticket(language="pt"))
        done.set_language.assert_awaited_once_with(900, "pt")
        confirmation = done.interaction.edit_original_response.await_args.kwargs["view"]
        self.assertEqual(texts(confirmation), "🌐 Idioma do suporte: Português.")
        self.assertIn("Olá <@100>!", texts(done.welcome.edit.await_args.kwargs["view"]))  # fetched and rewritten
        done.channel.send.assert_not_awaited()  # the confirmation is the only notice

    async def test_others_cannot_change_the_language(self):
        done = await self.change(member(555), views.LANG_SELECT_ID, ["es"])
        done.interaction.response.send_message.assert_awaited_once_with(views.MSG["no_permission"], ephemeral=True)
        done.set_language.assert_not_called()

    async def test_on_interaction_routes_the_language_controls(self):
        cog = make_cog()
        for custom_id in (views.LANG_SELECT_ID, "ticket_lang:es"):
            interaction = click(member(OWNER_ID), custom_id, views.language_picker("<@100>", ["es"]))
            with patch.object(cog, "on_language", AsyncMock()) as handler:
                await cog.on_interaction(interaction)
            handler.assert_awaited_once_with(interaction)

    async def test_set_language_sql_only_touches_open_tickets(self):
        pool = SimpleNamespace(fetchrow=AsyncMock(return_value=None))
        self.assertIsNone(await db.set_language(900, "es", pool=pool))
        self.assertIn("status = 'open'", pool.fetchrow.await_args.args[0])
        self.assertEqual(pool.fetchrow.await_args.args[1:], (900, "es"))


class OwnerReminderTests(unittest.IsolatedAsyncioTestCase):
    """A ticket waiting on its owner for OWNER_REMINDER_HOURS DMs them once."""

    async def test_reply_tracking_sql_writes_only_when_the_state_flips(self):
        pool = SimpleNamespace(execute=AsyncMock())
        await db.note_ticket_message(900, OWNER_ID, pool=pool)
        query, *args = pool.execute.await_args.args
        self.assertEqual(args, [900, OWNER_ID])
        self.assertIn("CASE WHEN owner_id = $2 THEN NULL ELSE now() END", query)
        self.assertIn("(owner_id = $2 AND awaiting_owner_since IS NOT NULL)", query)
        self.assertIn("(owner_id <> $2 AND awaiting_owner_since IS NULL)", query)
        self.assertIn("status = 'open'", query)

    async def test_claim_sql_takes_each_silence_once(self):
        pool = SimpleNamespace(fetch=AsyncMock(return_value=[]))
        self.assertEqual(await db.claim_owner_reminders(12, pool=pool), [])
        query, hours = pool.fetch.await_args.args
        self.assertEqual(hours, 12)
        self.assertIn("SET owner_reminded_at = now()", query)
        self.assertIn("owner_reminded_at IS NULL", query)
        self.assertIn("awaiting_owner_since <= now() - make_interval(hours => $1)", query)

    async def test_reopen_restarts_the_countdown(self):
        pool = SimpleNamespace(fetchrow=AsyncMock(return_value=None))
        await db.mark_reopened(900, pool=pool)
        self.assertIn("awaiting_owner_since = NULL, owner_reminded_at = NULL", pool.fetchrow.await_args.args[0])

    async def sweep(self, owner, tickets):
        cog, channel = make_cog(), make_channel(owner)
        channel.jump_url = "https://discord.com/channels/1/900"
        cog.bot.get_channel = lambda _id: channel if _id == 900 else None
        with patch.object(cog_module, "claim_owner_reminders", AsyncMock(return_value=tickets)) as claim:
            await cog._remind_owners()
        claim.assert_awaited_once_with(cog_module.OWNER_REMINDER_HOURS)

    async def test_sweep_dms_the_owner_in_the_ticket_language(self):
        owner = SimpleNamespace(id=OWNER_ID, send=AsyncMock())
        await self.sweep(owner, [make_ticket(language="es"), make_ticket(channel_id=901)])
        owner.send.assert_awaited_once()  # the ticket whose channel is gone is skipped
        dm = owner.send.await_args.kwargs["view"]
        self.assertIn("⏰ Ticket #0042: tu ticket espera tu respuesta", texts(dm))
        self.assertIn("https://discord.com/channels/1/900", [item.url for item in walk(dm) if isinstance(item, discord.ui.Button)])

    async def test_closed_dms_are_only_logged_at_info(self):
        response = SimpleNamespace(status=403, reason="Forbidden")
        owner = SimpleNamespace(id=OWNER_ID, send=AsyncMock(side_effect=discord.HTTPException(response, "DMs closed")))
        with self.assertLogs(cog_module.log, "INFO") as logs:
            await self.sweep(owner, [make_ticket()])
        self.assertEqual([record.levelname for record in logs.records], ["INFO"])
