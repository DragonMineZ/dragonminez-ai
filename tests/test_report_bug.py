import contextlib
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.cogs import ai_tickets
from bulmaai.cogs.ai_tickets import AITicketsCog
from bulmaai.services import ai_asks as ask_store
from bulmaai.ui import ai_asks
from v2_helpers import buttons, sent_message, texts

FORUM_ID = 500
GUILD_ID = 1
BOT = SimpleNamespace(id=999, bot=True, name="BulmaAI", display_name="BulmaAI")
REQUESTER = SimpleNamespace(id=42, bot=False, name="goku", display_name="Goku", mention="<@42>")
OTHER = SimpleNamespace(id=7, bot=False, name="vegeta", display_name="Vegeta", mention="<@7>")
SPEC = {
    "kind": "action",
    "action": "report_bug",
    "title": "Crash when transforming",
    "what_happened": "The game closes when I go Super Saiyan",
}
SUBMITTED = {"title": "Crash when transforming", "what_happened": "It closes <@1> @everyone", "version": "2.1.3"}
LINK = f"https://discord.com/channels/{GUILD_ID}/777"
NOW = datetime.now(timezone.utc)


def _row(spec=SPEC, *, answered_at=None, language="en") -> ask_store.AskRow:
    return ask_store.AskRow(
        message_id=55, channel_id=13, requester_id=42, spec=spec, language=language, created_at=NOW,
        answered_at=answered_at,
    )


def _file(attachment_id: int, filename: str, size: int = 100, content_type: str | None = None):
    return SimpleNamespace(
        id=attachment_id, filename=filename, size=size, content_type=content_type,
        read=AsyncMock(return_value=f"file {attachment_id}".encode()),
    )


def _message(message_id: int, author, files=(), *, minutes_ago: float = 1, components=()):
    return SimpleNamespace(
        id=message_id, author=author, attachments=list(files), snapshots=(), components=list(components),
        created_at=NOW - timedelta(minutes=minutes_ago),
    )


def _history(messages):
    async def history(limit=None, before=None, **_):
        for entry in sorted(messages, key=lambda m: m.created_at, reverse=True)[:limit]:
            yield entry

    return history


def _submitted(**values):
    return [{"type": 18, "component": {"type": 4, "custom_id": key, "value": value}} for key, value in values.items()]


class ReportBugTests(unittest.IsolatedAsyncioTestCase):
    def _cog(self, **settings) -> AITicketsCog:
        values = dict(
            ai_support_typing_lead_seconds=0,
            ai_support_enabled=True,
            ai_ticket_category_id=77,
            ticket_closed_category_id=None,
            ai_support_history_limit=10,
            discord_staff_role_ids=(),
            bug_report_forum_channel_id=FORUM_ID,
            bug_reports_enabled=True,
        )
        values.update(settings)
        self.starter = SimpleNamespace(attachments=["posted latest.log"])
        self.bugs = SimpleNamespace(triage_thread=AsyncMock(), _fetch_starter_message=AsyncMock(return_value=self.starter))
        self.thread = SimpleNamespace(id=777, name="Crash when transforming")
        self.members = {42: REQUESTER, 7: OTHER}
        self.forum = MagicMock(spec=discord.ForumChannel)
        self.forum.guild = SimpleNamespace(
            id=GUILD_ID,
            filesize_limit=1000,
            get_member=lambda user_id: self.members.get(user_id),
            fetch_member=AsyncMock(side_effect=discord.NotFound(SimpleNamespace(status=404, reason="x"), "no")),
        )
        self.forum.create_thread = AsyncMock(return_value=self.thread)
        bot = SimpleNamespace(
            settings=SimpleNamespace(**values),
            user=BOT,
            guilds=[],
            get_cog=lambda name: self.bugs if name == "BugReportsCog" else None,
            get_channel=lambda channel_id: self.forum if channel_id == FORUM_ID else None,
            fetch_channel=AsyncMock(side_effect=discord.NotFound(SimpleNamespace(status=404, reason="x"), "no")),
        )
        return AITicketsCog(bot)

    def _channel(self, messages=(), *, kind=discord.TextChannel, category_id: int = 5) -> MagicMock:
        channel = MagicMock(spec=kind)
        channel.id = 13
        channel.category = SimpleNamespace(id=category_id)
        channel.history = _history(list(messages))
        return channel

    def _interaction(self, channel, *, user=REQUESTER, submit: dict | None = None, spec=SPEC):
        card = sent_message(
            ai_tickets._card("This looks like a bug.", ai_tickets.AI_CARD_COLOR, *ai_asks.ask_rows(spec, 42, "en")),
            message_id=55,
            channel_id=13,
        )
        if submit is None:
            kind, data = discord.InteractionType.component, {"custom_id": "ai_ask:action:42:0"}
        else:
            kind, data = discord.InteractionType.modal_submit, {"custom_id": "ai_ask_bug:55", "components": _submitted(**submit)}
        return SimpleNamespace(
            type=kind,
            channel=channel,
            guild=None,
            user=user,
            data=data,
            message=card,
            response=SimpleNamespace(
                send_message=AsyncMock(), send_modal=AsyncMock(), defer=AsyncMock(), edit_message=AsyncMock()
            ),
            edit_original_response=AsyncMock(),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    @contextlib.contextmanager
    def _store(self, row, *, claimed: bool = True, ticket=None):
        with (
            patch.object(ai_tickets.ask_store, "get", AsyncMock(return_value=row)) as get,
            patch.object(ai_tickets.ask_store, "claim", AsyncMock(return_value=row if claimed else None)) as claim,
            patch.object(ai_tickets.ask_store, "release", AsyncMock()) as release,
            patch("bulmaai.cogs.ai_tickets.get_ticket_by_channel", AsyncMock(return_value=ticket)),
        ):
            yield SimpleNamespace(get=get, claim=claim, release=release)

    # --- the click ---

    async def test_the_click_opens_the_prefilled_form_and_claims_nothing(self) -> None:
        cog = self._cog()
        interaction = self._interaction(self._channel())
        with self._store(_row(language="pt")) as store:
            await cog.on_interaction(interaction)
        modal = interaction.response.send_modal.await_args.args[0]
        self.assertEqual((modal.custom_id, modal.title), ("ai_ask_bug:55", "Reportar um bug"))
        self.assertEqual([c.item.value for c in modal.children], [SPEC["title"], SPEC["what_happened"], None, None])
        store.claim.assert_not_called()
        interaction.message.edit.assert_not_called()
        interaction.response.send_message.assert_not_called()

    async def test_the_click_is_refused_when_the_report_cant_be_posted(self) -> None:
        closed = ai_asks.text("es", "bug_closed")
        for setup in (
            lambda cog: setattr(cog.bot.settings, "bug_report_forum_channel_id", None),  # no forum set
            lambda cog: setattr(cog.bot, "get_cog", lambda name: None),  # BugReportsCog not loaded
            lambda cog: setattr(cog.bot, "get_channel", lambda channel_id: None),  # the forum is gone
            lambda cog: self.members.pop(42),  # not in the forum's server
        ):
            cog = self._cog()
            setup(cog)
            interaction = self._interaction(self._channel())
            with self._store(_row(language="es")) as store:
                await cog.on_interaction(interaction)
            interaction.response.send_message.assert_awaited_once_with(closed, ephemeral=True)
            interaction.response.send_modal.assert_not_called()
            store.claim.assert_not_called()

    async def test_only_the_requester_and_never_in_a_ticket(self) -> None:
        cog = self._cog()
        interaction = self._interaction(self._channel(), user=OTHER)
        with self._store(_row()):
            await cog.on_interaction(interaction)
        interaction.response.send_message.assert_awaited_once_with(ai_asks.text("en", "not_yours"), ephemeral=True)
        interaction.response.send_modal.assert_not_called()

        interaction = self._interaction(self._channel(category_id=77))  # inside a ticket
        with self._store(_row(), ticket=SimpleNamespace(status="open")) as store:
            await cog.on_interaction(interaction)
        interaction.response.send_message.assert_awaited_once_with(ai_asks.text("en", "failed"), ephemeral=True)
        interaction.response.send_modal.assert_not_called()
        store.claim.assert_not_called()

        interaction = self._interaction(self._channel())
        with self._store(_row(answered_at=NOW)):
            await cog.on_interaction(interaction)
        interaction.response.send_message.assert_awaited_once_with(ai_asks.text("en", "answered"), ephemeral=True)

    async def test_a_reply_in_a_ticket_never_carries_it(self) -> None:
        cog = self._cog()
        base = {"tool_results": [], "confidence": 0.9, "suggested_close": False, "language": "en", "kind": "answer"}

        async def sent_ask(channel, *, in_ticket: bool):
            channel.created_at = NOW
            stand_in = ai_tickets._ChoiceMessage(SimpleNamespace(user=REQUESTER, channel=channel, guild=None), "Hi", 55)
            with (
                patch.object(cog, "_extract_image_context", AsyncMock(return_value={})),
                patch.object(cog, "_build_history", AsyncMock(return_value=[])),
                patch.object(cog, "_ticket_language", AsyncMock(return_value="en")),
                patch("bulmaai.cogs.ai_tickets.run_support_agent", AsyncMock(return_value={**base, "reply": "A bug.", "ask": SPEC})),
                patch("bulmaai.cogs.ai_tickets.can_post_publicly", return_value=True),
                patch.object(cog, "_send_messages_with_typing", AsyncMock(return_value=True)) as send,
                patch.object(cog, "_open_help_thread", AsyncMock(return_value=None)),
                patch.object(cog, "_awaiting_owner", AsyncMock()),
                patch.object(cog, "_is_ticket_owner", AsyncMock(return_value=False)),
            ):
                await cog._answer_support(stand_in, in_ticket=in_ticket, in_dm=False, mention_request=not in_ticket, member=None)
            return send.await_args.kwargs["ask"]

        self.assertIsNone(await sent_ask(self._channel(category_id=77), in_ticket=True))
        self.assertEqual(await sent_ask(self._channel(), in_ticket=False), SPEC)

    # --- the submit ---

    def _conversation(self):
        """Newest last: the requester's files, another member's log, and Bulma's repost of the requester's upload."""
        upload_card = sent_message(ai_asks.upload_card(42, ["latest.log"], [False]))
        return [
            _message(1, REQUESTER, [_file(11, "crash-report.txt", 300), _file(12, "shot.png", 200, "image/png")], minutes_ago=20),
            _message(2, OTHER, [_file(21, "their.log", 50)], minutes_ago=10),
            _message(3, BOT, [_file(31, "latest.log", 400)], minutes_ago=5, components=upload_card.components),
            _message(4, REQUESTER, [_file(41, "mods.zip", 10), _file(42, "big.png", 5000, "image/png"), _file(43, "notes.txt", 100)], minutes_ago=2),
        ]

    async def test_the_submit_posts_the_report_with_the_requesters_files_and_records_it(self) -> None:
        cog = self._cog()
        channel = self._channel(self._conversation())
        interaction = self._interaction(channel, submit=SUBMITTED)
        with self._store(_row()) as store:
            await cog.on_interaction(interaction)

        store.claim.assert_awaited_once_with(55)
        store.release.assert_not_called()
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, invisible=False)
        post = self.forum.create_thread.await_args.kwargs
        body = ai_asks.bug_post(42, ai_asks.bug_answers(_submitted(**SUBMITTED)))
        self.assertEqual((post["name"], post["content"]), ("Crash when transforming", body))
        self.assertTrue(body.startswith("Reported by <@42> with Bulma\n\n**What happened**\nIt closes"))
        self.assertNotIn("<@1>", body)
        self.assertEqual(post["allowed_mentions"].to_dict()["parse"], [])
        # Newest first, only logs and screenshots, at most 3 within the forum's upload limit (1000 bytes here):
        # notes.txt (100), the reposted latest.log (400), crash-report.txt (300); big.png is over the limit, shot.png
        # is the fourth, their.log isn't theirs.
        self.assertEqual([f.filename for f in post["files"]], ["notes.txt", "latest.log", "crash-report.txt"])

        edit = interaction.message.edit.await_args.kwargs
        self.assertEqual(texts(edit["view"])[-1], f"-# ↳ <@42>: **Reported the bug** → {LINK}")
        self.assertEqual((buttons(edit["view"]), edit["allowed_mentions"].to_dict()["parse"]), ([], []))
        interaction.followup.send.assert_awaited_once_with(
            ai_asks.text("en", "bug_done").format(link=LINK), ephemeral=True
        )
        self.bugs._fetch_starter_message.assert_awaited_once_with(self.thread)
        self.bugs.triage_thread.assert_awaited_once_with(
            self.thread, text=f"Crash when transforming\n\n{body}", reporter_id=42, attachments=["posted latest.log"]
        )
        # The card reads back as the requester's turn, without the link.
        card = SimpleNamespace(author=BOT, components=sent_message(edit["view"]).components, guild=None, edited_at=None)
        self.assertEqual(cog._choice_turn(card, 42)["content"], ai_asks.BUG_READBACK)

    async def test_a_dm_report_reads_the_dm_and_needs_the_forums_server(self) -> None:
        cog = self._cog()
        channel = self._channel(self._conversation()[:1], kind=discord.DMChannel)
        interaction = self._interaction(channel, submit=SUBMITTED)
        with self._store(_row()):
            await cog.on_interaction(interaction)
        files = self.forum.create_thread.await_args.kwargs["files"]
        self.assertEqual([f.filename for f in files], ["crash-report.txt", "shot.png"])

        self.members.pop(42)
        interaction = self._interaction(channel, submit=SUBMITTED)
        self.forum.create_thread.reset_mock()
        with self._store(_row()) as store:
            await cog.on_interaction(interaction)
        interaction.response.send_message.assert_awaited_once_with(ai_asks.text("en", "bug_closed"), ephemeral=True)
        store.claim.assert_not_called()
        self.forum.create_thread.assert_not_called()

    async def test_nothing_is_posted_while_bug_reports_are_off(self) -> None:
        cog = self._cog(bug_reports_enabled=False)
        interaction = self._interaction(self._channel(), submit=SUBMITTED)
        with self._store(_row()) as store:
            await cog.on_interaction(interaction)
        interaction.response.send_message.assert_awaited_once_with(ai_asks.text("en", "bug_closed"), ephemeral=True)
        store.claim.assert_not_called()
        self.forum.create_thread.assert_not_called()
        self.bugs.triage_thread.assert_not_called()

    async def test_a_lost_claim_posts_nothing(self) -> None:
        cog = self._cog()
        interaction = self._interaction(self._channel(), submit=SUBMITTED)
        with self._store(_row(), claimed=False):
            await cog.on_interaction(interaction)
        interaction.response.send_message.assert_awaited_once_with(ai_asks.text("en", "answered"), ephemeral=True)
        self.forum.create_thread.assert_not_called()

        interaction = self._interaction(self._channel(), submit=SUBMITTED, user=OTHER)
        with self._store(_row()) as store:
            await cog.on_interaction(interaction)
        interaction.response.send_message.assert_awaited_once_with(ai_asks.text("en", "not_yours"), ephemeral=True)
        store.claim.assert_not_called()

    async def test_a_failed_post_opens_the_offer_again(self) -> None:
        cog = self._cog()
        self.forum.create_thread = AsyncMock(side_effect=discord.HTTPException(SimpleNamespace(status=400, reason="x"), "tag required"))
        interaction = self._interaction(self._channel(), submit=SUBMITTED, spec=SPEC)
        with self._store(_row(language="es")) as store, self.assertLogs("bulmaai.cogs.ai_tickets", "ERROR"):
            await cog.on_interaction(interaction)
        store.claim.assert_awaited_once_with(55)
        store.release.assert_awaited_once_with(55)
        interaction.followup.send.assert_awaited_once_with(ai_asks.text("es", "failed"), ephemeral=True)
        interaction.message.edit.assert_not_called()
        self.bugs.triage_thread.assert_not_called()

    async def test_files_come_from_the_thread_or_the_window_before_the_card(self) -> None:
        cog = self._cog()
        thread = MagicMock(spec=discord.Thread)
        thread.id = 13
        conversation = self._conversation()
        with patch.object(cog, "_thread_context", AsyncMock(return_value=conversation)) as context:
            files = await cog._reporter_files(thread, 55, 42, 10_000)
        context.assert_awaited_once_with(thread)
        self.assertEqual([f.filename for f in files], ["big.png", "notes.txt", "latest.log"])  # a message's own order

        card_id = discord.utils.time_snowflake(NOW)
        old = _message(5, REQUESTER, [_file(51, "old.log")], minutes_ago=45)  # outside the 30 minutes before the card
        files = await cog._reporter_files(self._channel([old, *conversation[:1]]), card_id, 42, 10_000)
        self.assertEqual([f.filename for f in files], ["crash-report.txt", "shot.png"])
        self.assertEqual(files[0].fp.read(), b"file 11")


if __name__ == "__main__":
    unittest.main()
