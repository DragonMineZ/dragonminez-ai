import os
import types
import unittest
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch


os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.cogs.ai_tickets import (
    AITicketsCog,
    TICKET_TOOL_BOT_ID,
    _build_resolve_view,
    _ticket_tool_closer_id,
    _message_content,
    _parse_resolve_custom_id,
    _resolve_prompt_probability,
    _ticket_vector_store_id,
    _chunk_discord_message,
    _has_user_visible_tool_result,
    _is_ack_message,
    _is_pinging_bot,
    _is_reply_to_bot,
    _relative_age,
    _message_support_intent,
    _is_staff_ticket_message,
    _support_debounce_seconds,
)
from bulmaai.services.ticket_pages import StoredPage
from bulmaai.services.ticket_transcripts import TicketSummary, TranscriptLine
from bulmaai.services.support_intent import (
    SUPPORT_INTENT_PATREON_WHITELIST,
    SUPPORT_INTENT_SUPPORT_QUESTION,
    SUPPORT_INTENT_UNCLEAR,
)


class DiscordMessageChunkTests(unittest.TestCase):
    def test_chunks_long_messages_under_discord_limit(self) -> None:
        text = ("alpha beta gamma\n" * 180).strip()

        chunks = _chunk_discord_message(text, limit=500)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(0 < len(chunk) <= 500 for chunk in chunks))
        self.assertEqual("".join(chunks), text)

    def test_preserves_short_messages(self) -> None:
        self.assertEqual(_chunk_discord_message("Short answer."), ["Short answer."])

    def test_user_visible_tool_result_helper_still_detects_suppressed_tool_outputs(self) -> None:
        self.assertTrue(
            _has_user_visible_tool_result(
                [
                    {
                        "name": "manual_action",
                        "output": {"status": "ok", "suppress_ai_reply": True},
                    },
                ]
            )
        )

    def test_user_visible_tool_result_helper_ignores_non_suppressed_tool_outputs(self) -> None:
        self.assertFalse(
            _has_user_visible_tool_result(
                [{"name": "manual_action", "output": {"status": "needs_input"}}]
            )
        )

    def test_ack_messages_are_skipped_but_follow_ups_are_not(self) -> None:
        for ack in ("thanks!", "Ok", "gracias", "obrigado!!", "ty"):
            self.assertTrue(_is_ack_message(ack), ack)
        for follow_up in ("still broken", "1.20.1", "ok but it crashes again", "no funciona"):
            self.assertFalse(_is_ack_message(follow_up), follow_up)

    def test_relative_age_renders_compact_units(self) -> None:
        now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(_relative_age(datetime(2026, 9, 26, 11, 59, 30, tzinfo=timezone.utc), now), "just now")
        self.assertEqual(_relative_age(datetime(2026, 9, 26, 11, 55, tzinfo=timezone.utc), now), "5m ago")
        self.assertEqual(_relative_age(datetime(2026, 9, 26, 9, 0, tzinfo=timezone.utc), now), "3h ago")
        self.assertEqual(_relative_age(datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc), now), "2d ago")
        self.assertIsNone(_relative_age(None, now))

    def test_reply_to_bot_requires_a_reference_to_the_bot(self) -> None:
        bot_user = types.SimpleNamespace(id=999)
        to_bot = types.SimpleNamespace(
            reference=types.SimpleNamespace(resolved=types.SimpleNamespace(author=types.SimpleNamespace(id=999)))
        )
        to_other = types.SimpleNamespace(
            reference=types.SimpleNamespace(resolved=types.SimpleNamespace(author=types.SimpleNamespace(id=1)))
        )
        self.assertTrue(_is_reply_to_bot(to_bot, bot_user))
        self.assertFalse(_is_reply_to_bot(to_other, bot_user))
        self.assertFalse(_is_reply_to_bot(types.SimpleNamespace(reference=None), bot_user))

    def test_support_debounce_uses_configured_non_negative_value(self) -> None:
        self.assertEqual(
            _support_debounce_seconds(type("Settings", (), {"ai_support_debounce_seconds": 1.5})()),
            1.5,
        )
        self.assertEqual(
            _support_debounce_seconds(type("Settings", (), {"ai_support_debounce_seconds": -4})()),
            0.0,
        )
        self.assertEqual(_support_debounce_seconds(type("Settings", (), {})()), 0.0)

    def test_staff_ticket_messages_are_not_ai_support_triggers(self) -> None:
        role = type("Role", (), {"id": 44})()
        author = type("Author", (), {"bot": False, "roles": [role]})()
        message = type("Message", (), {"author": author})()
        settings = type("Settings", (), {"discord_staff_role_ids": (44,)})()

        self.assertTrue(_is_staff_ticket_message(message, in_ticket=True, settings=settings))
        self.assertFalse(_is_staff_ticket_message(message, in_ticket=False, settings=settings))

    def test_message_support_intent_strips_bot_mentions(self) -> None:
        bot_user = types.SimpleNamespace(id=999, mention="<@999>")
        message = types.SimpleNamespace(
            content="<@999> 20 + 20 + 20 + 7",
            attachments=[],
        )

        self.assertEqual(_message_support_intent(message, bot_user), SUPPORT_INTENT_UNCLEAR)

        message.content = "<@999> me das acceso patreon para la beta please"
        self.assertEqual(_message_support_intent(message, bot_user), SUPPORT_INTENT_PATREON_WHITELIST)

        message.content = "<@999> how do I configure dragon blocks?"
        self.assertEqual(_message_support_intent(message, bot_user), SUPPORT_INTENT_SUPPORT_QUESTION)

    def test_pinging_bot_detects_replies_to_bot_messages(self) -> None:
        bot_user = types.SimpleNamespace(id=999, mention="<@999>")
        referenced = types.SimpleNamespace(author=bot_user)
        message = types.SimpleNamespace(
            mentions=[],
            reference=types.SimpleNamespace(resolved=referenced, cached_message=None),
        )

        self.assertTrue(_is_pinging_bot(message, bot_user))


class ImageContextLatencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_extracts_multiple_image_contexts_concurrently(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 10
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        call_count = 0
        settings = types.SimpleNamespace(
            openai_vision_model="gpt-test",
            openai_support_max_output_tokens=100,
            ai_support_timeout_seconds=1,
        )
        bot = types.SimpleNamespace(settings=settings)
        cog = AITicketsCog(bot)
        attachments = [
            types.SimpleNamespace(id=1, filename="one.png", content_type="image/png", url="https://cdn.example/one.png"),
            types.SimpleNamespace(id=2, filename="two.png", content_type="image/png", url="https://cdn.example/two.png"),
        ]
        message = types.SimpleNamespace(attachments=attachments, channel=types.SimpleNamespace(id=55))
        prompts = []

        async def fake_create(**kwargs):
            nonlocal call_count
            call_count += 1
            prompts.append(kwargs["input"][0]["content"][0]["text"])
            if call_count == 1:
                first_started.set()
                await asyncio.wait_for(second_started.wait(), timeout=0.5)
            else:
                second_started.set()
                await asyncio.wait_for(first_started.wait(), timeout=0.5)
            return types.SimpleNamespace(output_text="image details")

        with (
            patch(
                "bulmaai.cogs.ai_tickets.vision_client.responses.create",
                new_callable=AsyncMock,
                side_effect=fake_create,
            ),
            patch("bulmaai.cogs.ai_tickets.get_image_analyses", new_callable=AsyncMock, return_value={}),
            patch("bulmaai.cogs.ai_tickets.save_image_analysis", new_callable=AsyncMock) as save,
        ):
            result = await asyncio.wait_for(cog._extract_image_context(message), timeout=1.0)

        self.assertEqual(result, {1: "image details", 2: "image details"})
        self.assertEqual(save.await_count, 2)
        self.assertTrue(prompts)
        self.assertTrue(all("whitelist" not in prompt.lower() for prompt in prompts))
        self.assertTrue(all("beta access" not in prompt.lower() for prompt in prompts))

    async def test_cached_image_analyses_are_not_reprocessed(self) -> None:
        settings = types.SimpleNamespace(
            openai_vision_model="gpt-test",
            openai_support_max_output_tokens=100,
            ai_support_timeout_seconds=1,
        )
        cog = AITicketsCog(types.SimpleNamespace(settings=settings))
        attachment = types.SimpleNamespace(id=7, filename="a.png", content_type="image/png", url="https://cdn/a.png")
        message = types.SimpleNamespace(attachments=[attachment], channel=types.SimpleNamespace(id=55))

        with (
            patch("bulmaai.cogs.ai_tickets.vision_client.responses.create", new_callable=AsyncMock) as create,
            patch("bulmaai.cogs.ai_tickets.get_image_analyses", new_callable=AsyncMock, return_value={7: "crash screen"}),
        ):
            result = await cog._extract_image_context(message)

        self.assertEqual(result, {7: "crash screen"})
        create.assert_not_awaited()


class TicketHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_ticket_history_uses_newest_messages_in_chronological_order(self) -> None:
        bot_user = types.SimpleNamespace(id=999, bot=True, name="BulmaAI", display_name="BulmaAI")
        settings = types.SimpleNamespace(ai_support_history_limit=2, discord_staff_role_ids=())
        cog = AITicketsCog(types.SimpleNamespace(settings=settings, user=bot_user))
        requester = types.SimpleNamespace(id=1, bot=False, name="goku", display_name="Goku")

        def entry(message_id: int, text: str):
            return types.SimpleNamespace(
                id=message_id,
                author=requester,
                clean_content=text,
                attachments=[],
                created_at=datetime.now(timezone.utc),
                reference=None,
            )

        newest_first = [entry(3, "third"), entry(2, "second"), entry(1, "first")]
        seen_kwargs: dict = {}

        async def history(**kwargs):
            seen_kwargs.update(kwargs)
            for item in newest_first[: kwargs["limit"]]:
                yield item

        channel = MagicMock(spec=discord.TextChannel)
        channel.history = history
        message = types.SimpleNamespace(channel=channel, author=requester)

        with patch("bulmaai.cogs.ai_tickets.get_image_analyses", new_callable=AsyncMock, return_value={}):
            history_items = await cog._build_ticket_history(message, {})

        self.assertNotIn("oldest_first", seen_kwargs)
        self.assertEqual([item["content"] for item in history_items], ["second", "third"])
        self.assertEqual(history_items[-1]["speaker_kind"], "requester")
        self.assertEqual(history_items[-1]["age"], "just now")


class ResolvePromptTests(unittest.IsolatedAsyncioTestCase):
    def test_probability_is_zero_below_floor_and_ramps_up(self) -> None:
        kwargs = {"min_confidence": 0.6, "exponent": 3.0}
        self.assertEqual(_resolve_prompt_probability(None, **kwargs), 0.0)
        self.assertEqual(_resolve_prompt_probability(0.59, **kwargs), 0.0)
        self.assertAlmostEqual(_resolve_prompt_probability(0.8, **kwargs), 0.512)
        self.assertEqual(_resolve_prompt_probability(1.0, **kwargs), 1.0)
        steps = [_resolve_prompt_probability(c / 100, **kwargs) for c in range(60, 101, 5)]
        self.assertEqual(steps, sorted(steps))

    async def test_resolve_buttons_round_trip_through_custom_id(self) -> None:
        view = _build_resolve_view(requester_id=42, language="es")
        parsed = [_parse_resolve_custom_id(item.custom_id) for item in view.children]
        self.assertEqual(parsed, [(True, 42, "es"), (False, 42, "es")])
        self.assertIsNone(_parse_resolve_custom_id("bug_issue:1"))
        self.assertIsNone(_parse_resolve_custom_id("ticket_resolved:yes:notanid:en"))

    def test_ticket_vector_store_falls_back_to_support_store(self) -> None:
        settings = types.SimpleNamespace(openai_ticket_vector_store_id=None, openai_support_vector_store_ids=("vs_a", "vs_b"))
        self.assertEqual(_ticket_vector_store_id(settings), "vs_a")
        settings.openai_ticket_vector_store_id = "vs_tickets"
        self.assertEqual(_ticket_vector_store_id(settings), "vs_tickets")
        settings = types.SimpleNamespace(openai_ticket_vector_store_id=None, openai_support_vector_store_ids=())
        self.assertIsNone(_ticket_vector_store_id(settings))

    def test_message_content_only_renders_previously_analyzed_images(self) -> None:
        message = types.SimpleNamespace(
            clean_content="it crashes",
            attachments=[
                types.SimpleNamespace(id=1, filename="seen.png", content_type="image/png"),
                types.SimpleNamespace(id=2, filename="unseen.png", content_type="image/png"),
                types.SimpleNamespace(id=3, filename="notes.pdf", content_type="application/pdf"),
            ],
        )
        content = _message_content(message, {1: "Crash screen: NullPointerException", 3: "ignored"})
        self.assertEqual(
            content,
            "it crashes\n[Image: Crash screen: NullPointerException]\n[Attachment] unseen.png\n[Attachment] notes.pdf",
        )


class TicketToolCloseDetectionTests(unittest.TestCase):
    def _message(self, author_id: int, *embeds: dict) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            author=types.SimpleNamespace(id=author_id),
            embeds=[types.SimpleNamespace(title=e.get("title"), description=e.get("description")) for e in embeds],
        )

    def test_detects_ticket_tool_close_embed_and_closer(self) -> None:
        message = self._message(TICKET_TOOL_BOT_ID, {"description": "Ticket Closed by <@!123>"})
        self.assertEqual(_ticket_tool_closer_id(message), 123)
        self.assertIsNone(_ticket_tool_closer_id(self._message(TICKET_TOOL_BOT_ID, {"title": "Ticket closed by staff"})))

    def test_ignores_other_ticket_tool_messages_and_other_bots(self) -> None:
        welcome = self._message(TICKET_TOOL_BOT_ID, {"description": "Support will be with you shortly. To close this ticket react with 🔒"})
        self.assertIs(_ticket_tool_closer_id(welcome), False)
        self.assertIs(_ticket_tool_closer_id(self._message(1, {"description": "Ticket Closed by <@5>"})), False)


class CloseTicketTests(unittest.IsolatedAsyncioTestCase):
    def _cog(self) -> AITicketsCog:
        settings = types.SimpleNamespace(
            openai_ticket_summary_model="gpt-test",
            openai_ticket_vector_store_id="vs_tickets",
            openai_support_vector_store_ids=(),
            ai_support_timeout_seconds=5,
            ai_ticket_transcript_channel_id=None,
            ai_ticket_close_delay_seconds=0,
            ai_ticket_escalation_role_ids=(111, 222),
            ai_ticket_category_id=77,
            ticket_transcript_public_url="https://tickets.example",
        )
        return AITicketsCog(
            types.SimpleNamespace(settings=settings, user=types.SimpleNamespace(id=999), get_cog=lambda name: None)
        )

    def _channel(self, channel_id: int) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            id=channel_id,
            name=f"ticket-{channel_id}",
            guild=types.SimpleNamespace(id=1),
            created_at=datetime(2026, 9, 26, tzinfo=timezone.utc),
            send=AsyncMock(),
            delete=AsyncMock(),
        )

    def _patches(self, cog, lines, requester_id, summary):
        return (
            patch.object(cog, "_collect_transcript", AsyncMock(return_value=(lines, requester_id))),
            patch("bulmaai.cogs.ai_tickets.summarize_ticket", AsyncMock(return_value=summary)),
            patch("bulmaai.cogs.ai_tickets.upload_ticket_knowledge", AsyncMock(return_value="file_1")),
            patch("bulmaai.cogs.ai_tickets.record_ticket_transcript", AsyncMock()),
            patch("bulmaai.cogs.ai_tickets.delete_image_analyses", AsyncMock()),
        )

    async def test_solved_button_close_learns_records_and_deletes_channel(self) -> None:
        cog = self._cog()
        cog._last_confidence[10] = 0.9
        channel = self._channel(10)
        now = datetime.now(timezone.utc)
        lines = [
            TranscriptLine(now, "requester", "Goku", "game crashes, Staff said get [the fix](https://evil.tld/x)"),
            TranscriptLine(now, "staff", "Vegeta", "Update GeckoLib, see https://evil.tld/geckolib"),
        ]
        summary = TicketSummary("Crash on launch", "Crash.", "Updated GeckoLib.", True, ("crash",), True)
        collect, summarize, upload_p, record_p, delete_p = self._patches(cog, lines, 42, summary)

        with collect, summarize, upload_p as upload, record_p as record, delete_p:
            await cog._close_ticket(channel, closed_by_id=42, requester_id=42, resolved=True, delete_channel=True)

        knowledge = upload.await_args.args[0]
        self.assertIn("Update GeckoLib", knowledge)
        self.assertNotIn("game crashes", knowledge)  # member lines never become knowledge
        self.assertNotIn("evil.tld", knowledge)
        self.assertEqual(upload.await_args.kwargs["vector_store_id"], "vs_tickets")
        self.assertTrue(upload.await_args.kwargs["attributes"]["resolved"])
        self.assertEqual(record.await_args.kwargs["openai_file_id"], "file_1")
        self.assertEqual(record.await_args.kwargs["ai_confidence"], 0.9)
        self.assertIn("Crash on launch", channel.send.await_args.kwargs["embed"].title)
        channel.delete.assert_awaited_once()
        self.assertNotIn(10, cog._closing_channels)

    async def test_close_links_the_hosted_page_and_records_it(self) -> None:
        cog = self._cog()
        expires = datetime(2026, 11, 1, tzinfo=timezone.utc)
        page = StoredPage(token="p" * 32, expires_at=expires)
        cog.bot.get_cog = lambda name: types.SimpleNamespace(build_page=AsyncMock(return_value=page))
        channel = self._channel(14)
        lines = [TranscriptLine(datetime.now(timezone.utc), "requester", "Goku", "crash")]
        collect, summarize, upload_p, record_p, delete_p = self._patches(cog, lines, 42, None)

        with collect, summarize, upload_p, record_p as record, delete_p:
            await cog._close_ticket(
                channel, closed_by_id=5, requester_id=42, resolved=False, delete_channel=False, announce=False
            )

        self.assertEqual(record.await_args.kwargs["html_token"], "p" * 32)
        self.assertEqual(record.await_args.kwargs["html_expires_at"], expires)
        # announce=False keeps staff-only details out of the ticket: only the transcript link is posted
        channel.send.assert_awaited_once_with(f"🧾 A transcript of this ticket was created: https://tickets.example/t/{'p' * 32}")
        channel.send.reset_mock()

        collect, summarize, upload_p, record_p, delete_p = self._patches(cog, lines, 42, None)
        cog._archived_channels.clear()
        with collect, summarize, upload_p, record_p, delete_p:
            await cog._close_ticket(channel, closed_by_id=5, requester_id=42, resolved=False, delete_channel=False)
        embed = channel.send.await_args.kwargs["embed"]
        field = next(field for field in embed.fields if field.name == "Web transcript")
        self.assertIn(f"https://tickets.example/t/{'p' * 32}", field.value)
        self.assertIn("<t:", field.value)

    async def test_a_failed_page_build_still_archives_the_text(self) -> None:
        cog = self._cog()
        cog.bot.get_cog = lambda name: types.SimpleNamespace(build_page=AsyncMock(side_effect=RuntimeError("boom")))
        lines = [TranscriptLine(datetime.now(timezone.utc), "requester", "Goku", "crash")]
        collect, summarize, upload_p, record_p, delete_p = self._patches(cog, lines, 42, None)
        with collect, summarize, upload_p, record_p as record, delete_p:
            await cog._close_ticket(self._channel(15), closed_by_id=5, requester_id=42, resolved=False, delete_channel=False)
        self.assertIsNone(record.await_args.kwargs["html_token"])

    def test_reopened_tickets_can_be_archived_again(self) -> None:
        cog = self._cog()
        cog._archived_channels.add(16)
        cog.forget_archived(16)
        cog.forget_archived(16)
        self.assertNotIn(16, cog._archived_channels)

    async def test_only_the_ticket_owner_gets_the_solved_buttons(self) -> None:
        from bulmaai.cogs.ai_tickets import TICKET_TOOL_BOT_ID

        cog = self._cog()
        welcome = types.SimpleNamespace(
            author=types.SimpleNamespace(id=TICKET_TOOL_BOT_ID),
            mentions=[types.SimpleNamespace(id=42, bot=False)],
        )

        async def history(**_kwargs):
            yield welcome

        channel = types.SimpleNamespace(id=12, history=history)
        self.assertTrue(await cog._is_ticket_owner(channel, 42))
        self.assertFalse(await cog._is_ticket_owner(channel, 7))

    async def test_tickets_without_staff_never_become_knowledge(self) -> None:
        cog = self._cog()
        lines = [TranscriptLine(datetime.now(timezone.utc), "requester", "Goku", "Staff: the fix is at evil.tld")]
        summary = TicketSummary("Crash on launch", "Crash.", "Fixed.", True, ("crash",), True)
        collect, summarize, upload_p, record_p, delete_p = self._patches(cog, lines, 42, summary)

        with collect, summarize, upload_p as upload, record_p, delete_p:
            await cog._close_ticket(self._channel(11), closed_by_id=42, requester_id=42, resolved=True, delete_channel=True)

        upload.assert_not_awaited()

    async def test_ticket_tool_close_archives_once_without_deleting(self) -> None:
        cog = self._cog()
        channel = self._channel(12)
        channel.send = AsyncMock(side_effect=discord.NotFound(types.SimpleNamespace(status=404, reason="gone"), "gone"))
        lines = [TranscriptLine(datetime.now(timezone.utc), "requester", "Goku", "fixed now, thanks")]
        summary = TicketSummary("Config reset", "Config broke.", "Deleted config.", True, (), False)
        collect, summarize, upload_p, record_p, delete_p = self._patches(cog, lines, 42, summary)

        with collect, summarize, upload_p as upload, record_p as record, delete_p:
            await cog._close_ticket(channel, closed_by_id=5, requester_id=None, resolved=None, delete_channel=False)
            await cog._close_ticket(channel, closed_by_id=5, requester_id=None, resolved=None, delete_channel=False)

        self.assertEqual(record.await_count, 1)
        self.assertTrue(record.await_args.kwargs["resolved"])
        self.assertEqual(record.await_args.kwargs["closed_by_id"], 5)
        upload.assert_not_awaited()
        channel.delete.assert_not_awaited()

    async def test_close_ticket_still_deletes_when_summary_fails(self) -> None:
        cog = self._cog()
        channel = self._channel(11)
        collect, _, upload_p, record_p, delete_p = self._patches(cog, [], None, None)
        with collect, patch("bulmaai.cogs.ai_tickets.summarize_ticket", AsyncMock(side_effect=RuntimeError("boom"))), upload_p as upload, record_p, delete_p:
            await cog._close_ticket(channel, closed_by_id=5, requester_id=None, resolved=False, delete_channel=True)

        upload.assert_not_awaited()
        channel.delete.assert_awaited_once()

    async def test_not_solved_button_escalates_and_pings_staff_roles(self) -> None:
        cog = self._cog()
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 13
        channel.category = types.SimpleNamespace(id=77)
        channel.send = AsyncMock()
        interaction = types.SimpleNamespace(
            channel=channel,
            user=types.SimpleNamespace(id=42),
            response=types.SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
        )

        with patch("bulmaai.cogs.ai_tickets.set_ticket_ai_disabled", AsyncMock()):
            await cog._handle_resolve_answer(interaction, solved=False, requester_id=42, language="en")

        interaction.response.edit_message.assert_awaited_once()
        self.assertIn(13, cog._escalated_ticket_channels)
        ping = channel.send.await_args
        self.assertIn("<@&111> <@&222>", ping.args[0])
        self.assertTrue(ping.kwargs["allowed_mentions"].roles)


if __name__ == "__main__":
    unittest.main()
