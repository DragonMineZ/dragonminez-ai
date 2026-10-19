import os
import types
import unittest
import asyncio
import contextlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch


os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.cogs import ai_tickets
from bulmaai.cogs.ai_tickets import (
    AITicketsCog,
    _build_resolve_view,
    _message_content,
    _parse_resolve_custom_id,
    _resolve_prompt_probability,
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
from bulmaai.services import ai_asks as ask_store
from bulmaai.services.ticket_intake import owner_says_solved
from bulmaai.ui import ai_asks
from bulmaai.ui import ticket_views
from bulmaai.ui.v2 import TEXT_LIMIT, WORKING
from v2_helpers import buttons, sent_message, texts, walk
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

    def test_requests_for_a_human_are_detected(self) -> None:
        for text in ("Might need an actual person's help for this unfort", "can I talk to a human", "quiero hablar con un humano"):
            self.assertTrue(ai_tickets._asks_for_human(text), text)
        for text in ("my human race texture is broken", "the person model is invisible", "still broken"):
            self.assertFalse(ai_tickets._asks_for_human(text), text)

    def test_owner_says_solved_reads_the_models_yes_no(self) -> None:
        for output_text, expected in (('{"solved": true}', True), ('{"solved": false}', False), ("{}", False)):
            response = types.SimpleNamespace(
                content=[types.SimpleNamespace(type="text", text=output_text)], stop_reason="end_turn"
            )
            client = types.SimpleNamespace(messages=types.SimpleNamespace(create=AsyncMock(return_value=response)))
            solved = asyncio.run(owner_says_solved("Owner: thank you, works a treat", model="claude-haiku-5-5", client=client))
            self.assertIs(solved, expected)
            kwargs = client.messages.create.await_args.kwargs
            self.assertIn("works a treat", kwargs["messages"][0]["content"])
            self.assertIn("system", kwargs)
            self.assertEqual(kwargs["output_config"]["format"]["type"], "json_schema")
            self.assertEqual(kwargs["output_config"]["effort"], "low")

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
            ai_vision_model="gpt-test",
            ai_support_max_output_tokens=100,
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
            content = kwargs["messages"][0]["content"]
            self.assertEqual(content[0]["type"], "image")
            self.assertEqual(content[0]["source"]["type"], "url")
            prompts.append(content[1]["text"])
            if call_count == 1:
                first_started.set()
                await asyncio.wait_for(second_started.wait(), timeout=0.5)
            else:
                second_started.set()
                await asyncio.wait_for(first_started.wait(), timeout=0.5)
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(type="text", text="image details")], stop_reason="end_turn"
            )

        with (
            patch(
                "bulmaai.cogs.ai_tickets.vision_client.messages.create",
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
            ai_vision_model="gpt-test",
            ai_support_max_output_tokens=100,
            ai_support_timeout_seconds=1,
        )
        cog = AITicketsCog(types.SimpleNamespace(settings=settings))
        attachment = types.SimpleNamespace(id=7, filename="a.png", content_type="image/png", url="https://cdn/a.png")
        message = types.SimpleNamespace(attachments=[attachment], channel=types.SimpleNamespace(id=55))

        with (
            patch("bulmaai.cogs.ai_tickets.vision_client.messages.create", new_callable=AsyncMock) as create,
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


class ClosedTranscriptTests(unittest.IsolatedAsyncioTestCase):
    async def test_messages_sent_while_closed_become_one_system_line(self) -> None:
        from bulmaai.services.ticket_transcripts import render_transcript_text

        def at(minute: int) -> datetime:
            return datetime(2026, 10, 1, 12, minute, tzinfo=timezone.utc)

        bot_user = types.SimpleNamespace(id=999, bot=True, name="BulmaAI", display_name="BulmaAI")
        owner = types.SimpleNamespace(id=1, bot=False, name="goku", display_name="Goku")
        helper = types.SimpleNamespace(id=2, bot=False, name="vegeta", display_name="Vegeta")

        def said(author, minute: int, text: str):
            return types.SimpleNamespace(author=author, clean_content=text, attachments=[], created_at=at(minute))

        oldest_first = [
            said(bot_user, 0, "welcome"), said(owner, 1, "it crashes"), said(bot_user, 2, "closed"),
            said(helper, 3, "zzhidden one"), said(helper, 4, "zzhidden two"), said(bot_user, 5, "reopened"),
            said(owner, 6, "thanks"),
        ]

        async def history(**kwargs):
            for item in oldest_first:
                yield item

        tickets = types.SimpleNamespace(closed_spans_for=AsyncMock(return_value=[(at(2), at(5))]))
        cogs = {"TicketsCog": tickets}
        settings = types.SimpleNamespace(discord_staff_role_ids=())
        cog = AITicketsCog(types.SimpleNamespace(settings=settings, user=bot_user, get_cog=cogs.get))
        channel = types.SimpleNamespace(id=12, history=history)

        lines, _ = await cog._collect_transcript(channel, 1)
        tickets.closed_spans_for.assert_awaited_once_with(12)
        self.assertEqual(
            [(line.speaker_kind, line.content) for line in lines],
            [
                ("assistant", "welcome"), ("requester", "it crashes"), ("assistant", "closed"),
                ("system", "2 messages were sent while this ticket was closed."), ("assistant", "reopened"),
                ("requester", "thanks"),
            ],
        )
        self.assertEqual(lines[3].created_at, at(3))
        text = render_transcript_text(lines, channel_name="closed-0042")
        self.assertIn("[2026-10-01 12:03 UTC] -- 2 messages were sent while this ticket was closed.", text)
        self.assertNotIn("zzhidden", text)

        cogs.clear()  # without the tickets cog nothing records closed stretches, so nothing is hidden
        lines, _ = await cog._collect_transcript(channel, 1)
        self.assertEqual(sum("zzhidden" in line.content for line in lines), 2)


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
        parsed = [_parse_resolve_custom_id(item.custom_id) for item in view.walk_children() if getattr(item, "custom_id", None)]
        self.assertEqual(parsed, [(True, 42, "es"), (False, 42, "es")])
        self.assertIsNone(_parse_resolve_custom_id("bug_issue:1"))
        self.assertIsNone(_parse_resolve_custom_id("ticket_resolved:yes:notanid:en"))

    async def test_resolve_prompt_pings_only_the_owner_in_the_ticket_language(self) -> None:
        settings = types.SimpleNamespace(
            ai_ticket_category_id=77, ai_ticket_resolve_min_confidence=0.6, ai_ticket_resolve_prompt_exponent=3.0
        )
        cog = AITicketsCog(types.SimpleNamespace(settings=settings))
        channel = types.SimpleNamespace(id=13, send=AsyncMock())
        with patch("bulmaai.cogs.ai_tickets._is_ticket_channel", return_value=True):
            await cog._maybe_prompt_resolution(channel, requester_id=42, confidence=None, force=True, language="pt")
        kwargs = channel.send.await_args.kwargs
        self.assertIn("<@42> **Resolvi o seu problema?**", str(kwargs["view"].to_components()))
        self.assertEqual([user.id for user in kwargs["allowed_mentions"].users], [42])
        safe = discord.AllowedMentions(everyone=False, roles=False, users=False)  # the client default (bot.SAFE_MENTIONS)
        self.assertEqual(safe.merge(kwargs["allowed_mentions"]).to_dict()["parse"], [])  # no roles or everyone
        self.assertIs(cog._resolve_prompts[13], channel.send.return_value)

    async def test_ticket_language_is_cached_until_forgotten_or_closed(self) -> None:
        cog = AITicketsCog(types.SimpleNamespace(settings=types.SimpleNamespace()))
        ticket = types.SimpleNamespace(language="es")
        with patch("bulmaai.cogs.ai_tickets.get_ticket_by_channel", AsyncMock(return_value=ticket)) as lookup:
            self.assertEqual([await cog._ticket_language(13), await cog._ticket_language(13)], ["es", "es"])
            self.assertEqual(lookup.await_count, 1)
            cog.forget_ticket_language(13)
            await cog._ticket_language(13)
            await cog.ticket_closed(13)
            await cog._ticket_language(13)
        self.assertEqual(lookup.await_count, 3)
        with patch("bulmaai.cogs.ai_tickets.get_ticket_by_channel", AsyncMock(return_value=None)):
            self.assertIsNone(await cog._ticket_language(14))
        self.assertIn(14, cog._ticket_languages)  # no tickets row: not looked up again

    def test_solved_close_text_has_every_language(self) -> None:
        self.assertEqual(set(ai_tickets.SOLVED_CLOSE_TEXT), set(ai_tickets.RESOLVE_PROMPT_TEXT))

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

    def test_message_content_inlines_the_parsed_log_not_the_file(self) -> None:
        message = types.SimpleNamespace(
            clean_content="game crashes on join",
            attachments=[types.SimpleNamespace(id=7, filename="latest.log", content_type="text/plain")],
        )
        self.assertEqual(
            _message_content(message, {7: "Environment: Forge 47.4.10"}),
            "game crashes on join\n[Log analysis of latest.log]\nEnvironment: Forge 47.4.10",
        )


class LogAnalysesTests(unittest.IsolatedAsyncioTestCase):
    def _message(self, attachment_id, filename, text):
        attachment = types.SimpleNamespace(
            id=attachment_id, filename=filename, size=len(text), read=AsyncMock(return_value=text.encode())
        )
        return types.SimpleNamespace(attachments=[attachment]), attachment

    async def test_logs_and_dumps_are_summarized_once_and_other_files_skipped(self) -> None:
        log_text = (
            "[19:07:39] [main/INFO] [cp.mo.mo.Launcher/MODLAUNCHER]: ModLauncher running: args [--fml.forgeVersion, 47.4.10]\n"
            "[19:07:40] [Render thread/ERROR] [dragonminez/]: Failed to load forms\n"
        )
        dump = "##############\n## DMZ DEBUG ##\n## Goku ##\n##############\n\n# Stats:\nSTR: 5\n"
        log_msg, log_att = self._message(9001, "latest.log", log_text)
        dump_msg, _ = self._message(9002, "Goku.log", dump)
        notes_msg, _ = self._message(9003, "notes.txt", "just some notes")

        analyses = await ai_tickets._log_analyses([log_msg, dump_msg, notes_msg])

        self.assertIn("Forge 47.4.10", analyses[9001])
        self.assertIn("Failed to load forms", analyses[9001])
        self.assertIn("Goku", analyses[9002])
        self.assertNotIn(9003, analyses)
        await ai_tickets._log_analyses([log_msg])
        log_att.read.assert_awaited_once()


class CloseTicketTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        knowledge = patch("bulmaai.cogs.ai_tickets.stored_knowledge_filename", AsyncMock(return_value=None))
        self.stored_knowledge_filename = knowledge.start()
        self.addCleanup(knowledge.stop)

    def _cog(self) -> AITicketsCog:
        settings = types.SimpleNamespace(
            ai_ticket_summary_model="gpt-test",
            ai_support_timeout_seconds=5,
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
            patch(
                "bulmaai.cogs.ai_tickets.save_ticket_knowledge",
                AsyncMock(side_effect=lambda markdown, **kwargs: kwargs["filename"]),
            ),
            patch("bulmaai.cogs.ai_tickets.record_ticket_transcript", AsyncMock()),
            patch("bulmaai.cogs.ai_tickets.delete_image_analyses", AsyncMock()),
        )

    async def test_solved_close_learns_and_records_without_posting_or_deleting(self) -> None:
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
            await cog._close_ticket(channel, closed_by_id=42, requester_id=42, resolved=True)

        knowledge = upload.await_args.args[0]
        self.assertIn("Update GeckoLib", knowledge)
        self.assertNotIn("game crashes", knowledge)  # member lines never become knowledge
        self.assertNotIn("evil.tld", knowledge)
        filename = upload.await_args.kwargs["filename"]
        self.assertRegex(filename, r"^ticket-\d{8}-10\.md$")
        self.assertEqual(upload.await_args.kwargs["title"], "Crash on launch")
        self.assertEqual(record.await_args.kwargs["knowledge_filename"], filename)
        self.assertEqual(record.await_args.kwargs["ai_confidence"], 0.9)
        # TicketsCog's closed card shows the outcome; the delete is its sweep's job.
        channel.send.assert_not_called()
        channel.delete.assert_not_called()
        self.assertNotIn(10, cog._closing_channels)

    async def test_reclose_overwrites_the_first_knowledge_doc_instead_of_adding_a_twin(self) -> None:
        cog = self._cog()
        self.stored_knowledge_filename.return_value = "ticket-20260901-16.md"
        now = datetime.now(timezone.utc)
        lines = [TranscriptLine(now, "staff", "Vegeta", "Update GeckoLib.")]
        summary = TicketSummary("Crash", "Crash.", "Updated GeckoLib.", True, ("crash",), True)
        collect, summarize, upload_p, record_p, delete_p = self._patches(cog, lines, 42, summary)

        with collect, summarize, upload_p as upload, record_p as record, delete_p:
            await cog._close_ticket(
                self._channel(16), closed_by_id=5, requester_id=42, resolved=True
            )

        self.assertEqual(upload.await_args.kwargs["filename"], "ticket-20260901-16.md")
        self.assertEqual(record.await_args.kwargs["knowledge_filename"], "ticket-20260901-16.md")

    async def test_close_links_the_hosted_page_and_records_it(self) -> None:
        cog = self._cog()
        expires = datetime(2026, 11, 1, tzinfo=timezone.utc)
        page = StoredPage(token="p" * 32, expires_at=expires)
        cog.bot.get_cog = lambda name: types.SimpleNamespace(build_page=AsyncMock(return_value=page))
        channel = self._channel(14)
        lines = [TranscriptLine(datetime.now(timezone.utc), "requester", "Goku", "crash")]
        collect, summarize, upload_p, record_p, delete_p = self._patches(cog, lines, 42, None)

        with collect, summarize, upload_p, record_p as record, delete_p:
            await cog._close_ticket(channel, closed_by_id=5, requester_id=42, resolved=False)

        self.assertEqual(record.await_args.kwargs["html_token"], "p" * 32)
        self.assertEqual(record.await_args.kwargs["html_expires_at"], expires)
        # Nothing is posted: TicketsCog's closed card carries the transcript link
        channel.send.assert_not_called()

    async def test_a_failed_page_build_still_archives_the_text(self) -> None:
        cog = self._cog()
        cog.bot.get_cog = lambda name: types.SimpleNamespace(build_page=AsyncMock(side_effect=RuntimeError("boom")))
        lines = [TranscriptLine(datetime.now(timezone.utc), "requester", "Goku", "crash")]
        collect, summarize, upload_p, record_p, delete_p = self._patches(cog, lines, 42, None)
        with collect, summarize, upload_p, record_p as record, delete_p:
            await cog._close_ticket(self._channel(15), closed_by_id=5, requester_id=42, resolved=False)
        self.assertIsNone(record.await_args.kwargs["html_token"])

    def test_reopened_tickets_can_be_archived_again(self) -> None:
        cog = self._cog()
        cog._archived_channels.add(16)
        cog.forget_archived(16)
        cog.forget_archived(16)
        self.assertNotIn(16, cog._archived_channels)

    async def test_only_the_ticket_owner_gets_the_solved_buttons(self) -> None:
        cog = self._cog()
        cog.bot.user = types.SimpleNamespace(id=1)  # our tickets cog posts the welcome that mentions the owner
        welcome = types.SimpleNamespace(
            author=types.SimpleNamespace(id=1),
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
            await cog._close_ticket(self._channel(11), closed_by_id=42, requester_id=42, resolved=True)

        upload.assert_not_awaited()

    async def test_close_archives_once_without_deleting(self) -> None:
        cog = self._cog()
        channel = self._channel(12)
        channel.send = AsyncMock(side_effect=discord.NotFound(types.SimpleNamespace(status=404, reason="gone"), "gone"))
        lines = [TranscriptLine(datetime.now(timezone.utc), "requester", "Goku", "fixed now, thanks")]
        summary = TicketSummary("Config reset", "Config broke.", "Deleted config.", True, (), False)
        collect, summarize, upload_p, record_p, delete_p = self._patches(cog, lines, 42, summary)

        with collect, summarize, upload_p as upload, record_p as record, delete_p:
            await cog._close_ticket(channel, closed_by_id=5, requester_id=None, resolved=None)
            await cog._close_ticket(channel, closed_by_id=5, requester_id=None, resolved=None)

        self.assertEqual(record.await_count, 1)
        self.assertTrue(record.await_args.kwargs["resolved"])
        self.assertEqual(record.await_args.kwargs["closed_by_id"], 5)
        upload.assert_not_awaited()
        channel.delete.assert_not_awaited()

    async def test_close_ticket_still_records_when_summary_fails(self) -> None:
        cog = self._cog()
        channel = self._channel(11)
        collect, _, upload_p, record_p, delete_p = self._patches(cog, [], None, None)
        with collect, patch("bulmaai.cogs.ai_tickets.summarize_ticket", AsyncMock(side_effect=RuntimeError("boom"))), upload_p as upload, record_p as record, delete_p:
            self.assertTrue(await cog._close_ticket(channel, closed_by_id=5, requester_id=None, resolved=False))

        upload.assert_not_awaited()
        record.assert_awaited_once()
        channel.delete.assert_not_called()

    def _open_ticket_channel(self, channel_id: int) -> MagicMock:
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = channel_id
        channel.category = types.SimpleNamespace(id=77)
        channel.send = AsyncMock()
        channel.delete = AsyncMock()
        return channel

    async def test_solved_button_closes_through_the_tickets_cog_and_never_deletes(self) -> None:
        cog = self._cog()
        tickets = types.SimpleNamespace(solve_ticket=AsyncMock(return_value=object()))
        cog.bot.get_cog = lambda name: tickets if name == "TicketsCog" else None
        channel = self._open_ticket_channel(13)
        prompt = ai_tickets._build_resolve_view(42, "es")
        cog._resolve_prompts[13] = types.SimpleNamespace(delete=AsyncMock())
        interaction = types.SimpleNamespace(
            channel=channel,
            user=types.SimpleNamespace(id=42),
            data={"custom_id": f"{ai_tickets.RESOLVE_BUTTON_PREFIX}yes:42:es"},
            message=sent_message(prompt, channel_id=13),
            response=types.SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
            edit_original_response=AsyncMock(),
            followup=types.SimpleNamespace(send=AsyncMock()),
        )

        with (
            patch("bulmaai.cogs.ai_tickets.get_ticket_by_channel", AsyncMock(return_value=None)),
            patch.object(cog, "_close_ticket", AsyncMock()) as archive,
        ):
            await cog._handle_resolve_answer(interaction, solved=True, requester_id=42, language="es")

        tickets.solve_ticket.assert_awaited_once_with(channel, closer_id=42)
        archive.assert_not_called()  # only through TicketsCog's close (which archives)
        final = interaction.edit_original_response.await_args.kwargs["view"]
        self.assertIn("Me alegra haber ayudado", str(final.to_components()))
        # Popped before the close, so ticket_closed doesn't delete the prompt that now says thanks.
        self.assertNotIn(13, cog._resolve_prompts)
        channel.delete.assert_not_called()
        interaction.followup.send.assert_not_called()
        self.assertEqual(cog._working, set())

    async def test_solved_button_without_the_tickets_cog_fails_visibly(self) -> None:
        cog = self._cog()
        channel = self._open_ticket_channel(13)
        interaction = types.SimpleNamespace(
            channel=channel,
            user=types.SimpleNamespace(id=42),
            data={"custom_id": f"{ai_tickets.RESOLVE_BUTTON_PREFIX}yes:42:en"},
            message=sent_message(ai_tickets._build_resolve_view(42, "en"), channel_id=13),
            response=types.SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
            edit_original_response=AsyncMock(),
            followup=types.SimpleNamespace(send=AsyncMock()),
        )
        with patch("bulmaai.cogs.ai_tickets.get_ticket_by_channel", AsyncMock(return_value=None)):
            await cog._handle_resolve_answer(interaction, solved=True, requester_id=42, language="en")
        interaction.followup.send.assert_awaited_once_with(ai_tickets.RESOLVE_FAILED_TEXT, ephemeral=True)
        channel.delete.assert_not_called()

    async def test_owner_saying_it_is_solved_closes_through_the_tickets_cog(self) -> None:
        cog = self._cog()
        tickets = types.SimpleNamespace(solve_ticket=AsyncMock(return_value=object()))
        cog.bot.get_cog = lambda name: tickets if name == "TicketsCog" else None
        channel = self._open_ticket_channel(14)

        async def history(**_kwargs):
            if False:
                yield

        channel.history = history
        message = types.SimpleNamespace(content="works now, thanks!", author=types.SimpleNamespace(id=42))
        with (
            patch.object(cog, "_is_ticket_owner", AsyncMock(return_value=True)),
            patch("bulmaai.cogs.ai_tickets.owner_says_solved", AsyncMock(return_value=True)),
            patch("bulmaai.cogs.ai_tickets.ai_budget.is_paused", return_value=False),
            patch("bulmaai.cogs.ai_tickets.get_ticket_by_channel", AsyncMock(return_value=None)),
            patch.object(cog, "_close_ticket", AsyncMock()) as archive,
        ):
            await cog._maybe_close_solved(channel, message)

        self.assertIn("Glad it's sorted", str(channel.send.await_args.kwargs["view"].to_components()))
        tickets.solve_ticket.assert_awaited_once_with(channel, closer_id=42)
        archive.assert_not_called()
        channel.delete.assert_not_called()

    async def test_not_solved_button_escalates_and_pings_staff_roles(self) -> None:
        cog = self._cog()
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 13
        channel.category = types.SimpleNamespace(id=77)
        channel.send = AsyncMock()
        prompt = ai_tickets._build_resolve_view(42, "en")
        no_id = f"{ai_tickets.RESOLVE_BUTTON_PREFIX}no:42:en"
        interaction = types.SimpleNamespace(
            channel=channel,
            user=types.SimpleNamespace(id=42),
            data={"custom_id": no_id},
            message=sent_message(prompt, channel_id=13),
            response=types.SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
            edit_original_response=AsyncMock(),
            followup=types.SimpleNamespace(send=AsyncMock()),
        )

        with (
            patch("bulmaai.cogs.ai_tickets.set_ticket_ai_disabled", AsyncMock()),
            patch("bulmaai.cogs.ai_tickets.get_ticket_by_channel", AsyncMock(return_value=None)),
        ):
            await cog._handle_resolve_answer(interaction, solved=False, requester_id=42, language="en")

        busy = interaction.response.edit_message.await_args.kwargs["view"]
        clicked = [item for item in busy.walk_children() if isinstance(item, discord.ui.Button)]
        self.assertEqual([(b.label, b.disabled) for b in clicked], [("Yes, it's solved", True), (WORKING, True)])
        final = interaction.edit_original_response.await_args.kwargs["view"]
        self.assertIn("flagged this ticket for staff", str(final.to_components()))
        self.assertEqual(cog._working, set())
        self.assertIn(13, cog._escalated_ticket_channels)
        ping = channel.send.await_args
        self.assertIn("<@&111> <@&222>", ping.args[0])
        self.assertTrue(ping.kwargs["allowed_mentions"].roles)


CHOICES = {"kind": "choices", "choices": ["Stats", "Raza"]}
SELECT = {"kind": "select", "options": [{"label": "1.20.1"}, {"label": "1.21"}]}
FORM = {
    "kind": "form",
    "title": "Your setup",
    "fields": [
        {"label": "Version", "type": "select", "options": ["1.20.1", "1.21"], "required": True},
        {"label": "What happens", "type": "long", "required": False},
        {"label": "Mods", "type": "short", "required": False},
    ],
}
UPLOAD = {"kind": "file", "what": "log", "max_files": 2}
STEPS = {"kind": "steps", "steps": ["Close Minecraft", "Delete the config folder", "Start the game"]}
GET_STAFF = {"kind": "action", "action": "get_staff"}


def _ask_row(spec, *, requester_id: int = 42, language: str = "en", answered_at=None, progress: int = 0) -> ask_store.AskRow:
    return ask_store.AskRow(
        message_id=55,
        channel_id=13,
        requester_id=requester_id,
        spec=spec,
        language=language,
        created_at=datetime.now(timezone.utc),
        answered_at=answered_at,
        progress=progress,
    )


class AnswerControlsTests(unittest.IsolatedAsyncioTestCase):
    def _cog(self, **settings) -> AITicketsCog:
        values = dict(
            ai_support_typing_lead_seconds=0,
            ai_support_enabled=True,
            ai_ticket_category_id=77,
            ticket_closed_category_id=None,
            ai_support_history_limit=10,
            discord_staff_role_ids=(),
        )
        values.update(settings)
        self.bot_user = types.SimpleNamespace(id=999, bot=True, name="BulmaAI", display_name="BulmaAI")
        return AITicketsCog(types.SimpleNamespace(settings=types.SimpleNamespace(**values), user=self.bot_user, guilds=[]))

    def _card(self, spec):
        return ai_tickets._card("What do you want to change?", ai_tickets.AI_CARD_COLOR, *ai_asks.ask_rows(spec, 42, "en"))

    def _answered(self, lines):
        card = ai_tickets._card(f"What do you want to change?\n\n{ai_tickets.AI_FOOTER}")
        return sent_message(ai_tickets._answered_card(sent_message(card), lines), message_id=55)

    def _picked(self, user_id: int = 42, label: str = "Stats"):
        return self._answered([ai_asks.pick_line(user_id, label)])

    @contextlib.contextmanager
    def _store(self, row, *, claimed: bool = True):
        with (
            patch.object(ai_tickets.ask_store, "get", AsyncMock(return_value=row)) as get,
            patch.object(ai_tickets.ask_store, "claim", AsyncMock(return_value=row if claimed else None)) as claim,
            patch.object(ai_tickets.ask_store, "release", AsyncMock()) as release,
        ):
            yield types.SimpleNamespace(get=get, claim=claim, release=release)

    async def test_only_the_last_chunk_gets_the_controls_and_its_spec_is_saved(self) -> None:
        cog = self._cog()
        channel = types.SimpleNamespace(
            id=13, send=AsyncMock(side_effect=[types.SimpleNamespace(id=1), types.SimpleNamespace(id=2)])
        )
        long_text = "word " * 1000  # two cards

        with patch.object(ai_tickets.ask_store, "save", AsyncMock()) as save:
            self.assertTrue(
                await cog._send_messages_with_typing(
                    channel, [long_text], ai_answer=True, ask=CHOICES, requester_id=42, language="es"
                )
            )
            first, last = (call.kwargs["view"] for call in channel.send.await_args_list)
            self.assertEqual(buttons(first), [])
            self.assertEqual(
                [(b.label, b.custom_id, b.style) for b in buttons(last)],
                [
                    ("Stats", "ai_ask:choices:42:0", discord.ButtonStyle.secondary),
                    ("Raza", "ai_ask:choices:42:1", discord.ButtonStyle.secondary),
                ],
            )
            self.assertIn(ai_tickets.AI_FOOTER, texts(last)[0])
            save.assert_awaited_once_with(message_id=2, channel_id=13, requester_id=42, spec=CHOICES, language="es")

            save.reset_mock()
            channel.send = AsyncMock(return_value=types.SimpleNamespace(id=3))
            await cog._send_messages_with_typing(channel, ["Which one?"], ai_answer=True, ask=CHOICES)
            self.assertEqual(buttons(channel.send.await_args.kwargs["view"]), [])  # no requester, no controls
            save.assert_not_called()

    async def test_select_and_form_controls_and_a_failed_save_still_sends(self) -> None:
        cog = self._cog()
        channel = types.SimpleNamespace(id=13, send=AsyncMock(return_value=types.SimpleNamespace(id=2)))
        with (
            patch.object(ai_tickets.ask_store, "save", AsyncMock(side_effect=RuntimeError("db down"))),
            self.assertLogs(ai_tickets.log, "ERROR"),
        ):
            self.assertTrue(
                await cog._send_messages_with_typing(channel, ["Which version?"], ai_answer=True, ask=SELECT, requester_id=42)
            )
        (select,) = [item for item in walk(channel.send.await_args.kwargs["view"]) if isinstance(item, discord.ui.Select)]
        self.assertEqual((select.custom_id, [o.label for o in select.options]), ("ai_ask:select:42:0", ["1.20.1", "1.21"]))

        with patch.object(ai_tickets.ask_store, "save", AsyncMock()):
            await cog._send_messages_with_typing(channel, ["Tell me more."], ai_answer=True, ask=FORM, requester_id=42, language="pt")
        (button,) = buttons(channel.send.await_args.kwargs["view"])
        self.assertEqual((button.label, button.custom_id), ("Preencher", "ai_ask:form:42:0"))

    async def test_the_answered_card_keeps_its_text_and_records_the_answer(self) -> None:
        picked = self._picked()
        self.assertEqual(ai_tickets._card_text(picked), "What do you want to change?")
        self.assertEqual(ai_tickets._card_answer(picked), (42, "Stats"))
        self.assertEqual(ai_tickets._card_answer(sent_message(ai_tickets._card("No pick."))), None)
        view = discord.ui.DesignerView.from_message(picked)
        self.assertEqual(buttons(view), [])
        self.assertEqual(texts(view)[-1], "-# ↳ <@42>: **Stats**")

    async def test_choice_turn_is_the_answerers_turn(self) -> None:
        cog = self._cog()
        guild = types.SimpleNamespace(get_member=lambda user_id: types.SimpleNamespace(display_name="Goku (roles: staff)"))
        entry = types.SimpleNamespace(author=self.bot_user, components=self._picked().components, guild=guild, edited_at=None)
        turn = cog._choice_turn(entry, 42)
        self.assertEqual(
            (turn["role"], turn["content"], turn["speaker_kind"], turn["speaker_id"], turn["speaker_name"]),
            ("user", "Stats", "requester", "42", "Goku roles staff"),
        )
        self.assertEqual(cog._choice_turn(entry, 7)["speaker_kind"], "participant")
        entry.guild = None
        self.assertEqual(cog._choice_turn(entry, 42)["speaker_name"], "member")
        entry.components = self._answered(ai_asks.form_lines(42, [("Version", "1.21"), ("What happens", "It crashes")])).components
        self.assertEqual(cog._choice_turn(entry, 42)["content"], "Version: 1.21\nWhat happens: It crashes")
        entry.author = types.SimpleNamespace(id=1, bot=False)
        self.assertIsNone(cog._choice_turn(entry, 42))  # only our own cards

    async def test_history_and_transcript_read_the_answer_as_the_latest_requester_turn(self) -> None:
        cog = self._cog()
        requester = types.SimpleNamespace(id=42, bot=False, name="goku", display_name="Goku")
        now = datetime.now(timezone.utc)
        question = types.SimpleNamespace(
            id=1, author=requester, clean_content="I want to change my character", attachments=[], created_at=now,
            reference=None, guild=None,
        )
        card = types.SimpleNamespace(
            id=55, author=self.bot_user, clean_content="", attachments=[], created_at=now, reference=None, guild=None,
            components=self._picked().components, edited_at=now, mentions=[],
        )

        async def history(**kwargs):
            for item in ([question, card] if kwargs.get("oldest_first") else [card, question]):
                yield item

        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 13
        channel.history = history
        with patch("bulmaai.cogs.ai_tickets.get_image_analyses", new_callable=AsyncMock, return_value={}):
            turns = await cog._build_ticket_history(types.SimpleNamespace(channel=channel, author=requester), {})
            cog.bot.get_cog = lambda name: None
            lines, _ = await cog._collect_transcript(channel, 42)

        self.assertEqual(
            [(turn["role"], turn["content"]) for turn in turns],
            [("user", "I want to change my character"), ("assistant", "What do you want to change?"), ("user", "Stats")],
        )
        self.assertEqual(turns[-1]["speaker_kind"], "requester")
        self.assertEqual(
            [(line.speaker_kind, line.content) for line in lines],
            [("requester", "I want to change my character"), ("assistant", "What do you want to change?"), ("requester", "Stats")],
        )

    def _interaction(self, channel, user_id: int, *, spec=CHOICES, custom_id: str = "ai_ask:choices:42:1", **data):
        return types.SimpleNamespace(
            type=discord.InteractionType.component,
            channel=channel,
            guild=None,
            user=types.SimpleNamespace(id=user_id, mention=f"<@{user_id}>"),
            data={"custom_id": custom_id, **data},
            message=sent_message(self._card(spec), message_id=55, channel_id=13),
            response=types.SimpleNamespace(
                edit_message=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock(), defer=AsyncMock()
            ),
            edit_original_response=AsyncMock(),
            followup=types.SimpleNamespace(send=AsyncMock()),
        )

    def _ticket_channel(self) -> MagicMock:
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 13
        channel.category = types.SimpleNamespace(id=77)
        return channel

    def _open_ticket(self):
        return patch("bulmaai.cogs.ai_tickets.get_ticket_by_channel", AsyncMock(return_value=types.SimpleNamespace(status="open")))

    async def test_only_the_asked_member_can_answer_and_hears_it_in_the_cards_language(self) -> None:
        cog = self._cog()
        interaction = self._interaction(self._ticket_channel(), user_id=7)
        with self._store(_ask_row(CHOICES, language="es")) as store:
            await cog.on_interaction(interaction)
        interaction.response.send_message.assert_awaited_once_with(ai_asks.text("es", "not_yours"), ephemeral=True)
        interaction.response.edit_message.assert_not_called()
        store.claim.assert_not_called()

    async def test_a_card_without_its_row_has_expired(self) -> None:
        cog = self._cog()
        interaction = self._interaction(self._ticket_channel(), user_id=42)
        with self._store(None):
            await cog.on_interaction(interaction)
        interaction.response.send_message.assert_awaited_once_with(
            "This question expired, type your answer instead.", ephemeral=True
        )

    async def test_answered_closed_or_ai_off_refuse_the_answer(self) -> None:
        cog = self._cog()
        interaction = self._interaction(self._ticket_channel(), user_id=42)
        with self._store(_ask_row(CHOICES, answered_at=datetime.now(timezone.utc))):
            await cog._handle_ask(interaction, kind="choices", index=1)
        interaction.response.send_message.assert_awaited_once_with("Already answered.", ephemeral=True)

        interaction = self._interaction(self._ticket_channel(), user_id=42)
        closed = types.SimpleNamespace(status="closed")
        with self._store(_ask_row(CHOICES)), patch("bulmaai.cogs.ai_tickets.get_ticket_by_channel", AsyncMock(return_value=closed)):
            await cog._handle_ask(interaction, kind="choices", index=1)
        interaction.response.send_message.assert_awaited_once_with(ai_asks.text("en", "closed"), ephemeral=True)

        interaction = self._interaction(self._ticket_channel(), user_id=42)
        cog._escalated_ticket_channels.add(13)
        with self._store(_ask_row(CHOICES, language="pt")) as store, self._open_ticket():
            await cog._handle_ask(interaction, kind="choices", index=1)
        interaction.response.send_message.assert_awaited_once_with(ai_asks.text("pt", "ai_off"), ephemeral=True)
        interaction.response.edit_message.assert_not_called()
        store.claim.assert_not_called()

    async def test_pick_records_on_the_card_and_answers_as_the_owners_turn(self) -> None:
        cog = self._cog()
        channel = self._ticket_channel()
        interaction = self._interaction(channel, user_id=42)
        with (
            self._store(_ask_row(CHOICES)) as store,
            self._open_ticket(),
            patch("bulmaai.cogs.ai_tickets.ai_budget.is_paused", return_value=False),
            patch("bulmaai.cogs.ai_tickets.note_ticket_message", AsyncMock()) as noted,
            patch.object(cog, "_answer_support", AsyncMock()) as answer,
        ):
            await cog.on_interaction(interaction)
            await cog._pending_tasks[(13, 0)]

        busy = interaction.response.edit_message.await_args.kwargs["view"]
        self.assertEqual([(b.label, b.disabled) for b in buttons(busy)], [("Stats", True), (WORKING, True)])
        store.claim.assert_awaited_once_with(55)
        final = interaction.edit_original_response.await_args.kwargs
        self.assertEqual(buttons(final["view"]), [])
        self.assertEqual(texts(final["view"]), ["What do you want to change?", "-# ↳ <@42>: **Raza**"])
        self.assertEqual(final["allowed_mentions"].to_dict()["parse"], [])
        noted.assert_awaited_once_with(13, 42)  # the owner replied
        stand_in = answer.await_args.args[0]
        self.assertEqual((stand_in.content, stand_in.id, stand_in.author.id), ("Raza", 55, 42))
        self.assertEqual(
            answer.await_args.kwargs, {"in_ticket": True, "in_dm": False, "mention_request": False, "member": None}
        )
        self.assertEqual((cog._pending_tasks, cog._working), ({}, set()))
        interaction.followup.send.assert_not_called()

    async def test_a_lost_claim_only_takes_the_controls_off(self) -> None:
        cog = self._cog()
        interaction = self._interaction(self._ticket_channel(), user_id=42)
        with (
            self._store(_ask_row(CHOICES), claimed=False) as store,
            self._open_ticket(),
            patch("bulmaai.cogs.ai_tickets.ai_budget.is_paused", return_value=False),
            patch.object(cog, "_answer_support", AsyncMock()) as answer,
        ):
            await cog._handle_ask(interaction, kind="choices", index=1)
        final = interaction.edit_original_response.await_args.kwargs["view"]
        self.assertEqual((buttons(final), texts(final)), ([], ["What do you want to change?"]))
        interaction.followup.send.assert_awaited_once_with("Already answered.", ephemeral=True)
        answer.assert_not_called()
        store.release.assert_not_called()

    async def test_select_pick_reads_the_submitted_value(self) -> None:
        cog = self._cog()
        interaction = self._interaction(self._ticket_channel(), user_id=42, spec=SELECT, custom_id="ai_ask:select:42:0", values=["1"])
        with (
            self._store(_ask_row(SELECT)),
            self._open_ticket(),
            patch("bulmaai.cogs.ai_tickets.ai_budget.is_paused", return_value=False),
            patch("bulmaai.cogs.ai_tickets.note_ticket_message", AsyncMock()),
            patch.object(cog, "_answer_support", AsyncMock()) as answer,
        ):
            await cog.on_interaction(interaction)
            await cog._pending_tasks[(13, 0)]
        self.assertEqual(texts(interaction.edit_original_response.await_args.kwargs["view"])[-1], "-# ↳ <@42>: **1.21**")
        self.assertEqual(answer.await_args.args[0].content, "1.21")

    async def test_fill_in_opens_the_form_without_claiming_or_spending(self) -> None:
        cog = self._cog()
        interaction = self._interaction(self._ticket_channel(), user_id=42, spec=FORM, custom_id="ai_ask:form:42:0")
        with (
            self._store(_ask_row(FORM)) as store,
            self._open_ticket(),
            patch("bulmaai.cogs.ai_tickets.ai_budget.is_paused", return_value=False),
        ):
            await cog.on_interaction(interaction)
        modal = interaction.response.send_modal.await_args.args[0]
        self.assertEqual((modal.custom_id, modal.title), ("ai_ask_form:55", "Your setup"))
        self.assertEqual([child.label for child in modal.children], ["Version", "What happens", "Mods"])
        store.claim.assert_not_called()
        interaction.response.edit_message.assert_not_called()
        self.assertEqual(dict(cog._support_events), {})  # opening the form runs no AI

    def _submit(self, channel, user_id: int = 42, components=None):
        return types.SimpleNamespace(
            type=discord.InteractionType.modal_submit,
            channel=channel,
            guild=None,
            user=types.SimpleNamespace(id=user_id, mention=f"<@{user_id}>"),
            data={
                "custom_id": "ai_ask_form:55",
                "components": components
                if components is not None
                else [
                    {"type": 18, "component": {"type": 3, "custom_id": "f0", "values": ["1"]}},
                    {"type": 18, "component": {"type": 4, "custom_id": "f1", "value": "It crashes\nwhen I fly"}},
                    {"type": 18, "component": {"type": 4, "custom_id": "f2", "value": ""}},
                ],
            },
            message=None,
            response=types.SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=types.SimpleNamespace(send=AsyncMock()),
        )

    async def test_form_submit_records_one_line_per_field_and_answers(self) -> None:
        cog = self._cog()
        channel = self._ticket_channel()
        card = sent_message(self._card(FORM), message_id=55, channel_id=13)
        channel.fetch_message = AsyncMock(return_value=card)
        interaction = self._submit(channel)
        with (
            self._store(_ask_row(FORM)) as store,
            self._open_ticket(),
            patch("bulmaai.cogs.ai_tickets.ai_budget.is_paused", return_value=False),
            patch("bulmaai.cogs.ai_tickets.note_ticket_message", AsyncMock()) as noted,
            patch.object(cog, "_answer_support", AsyncMock()) as answer,
        ):
            await cog.on_interaction(interaction)
            await cog._pending_tasks[(13, 0)]

        store.claim.assert_awaited_once_with(55)
        interaction.response.defer.assert_awaited_once_with(invisible=True)
        channel.fetch_message.assert_awaited_once_with(55)
        edit = card.edit.await_args.kwargs
        self.assertEqual(buttons(edit["view"]), [])
        self.assertEqual(
            texts(edit["view"]),
            [
                "What do you want to change?",
                "-# ↳ <@42> · Version: **1.21**",
                "-# ↳ <@42> · What happens: **It crashes when I fly**",
            ],
        )
        self.assertEqual(edit["allowed_mentions"].to_dict()["parse"], [])
        noted.assert_awaited_once_with(13, 42)
        stand_in = answer.await_args.args[0]
        self.assertEqual((stand_in.content, stand_in.id), ("Version: 1.21\nWhat happens: It crashes when I fly", 55))
        self.assertEqual(len(cog._support_events[(42, 0)]), 1)  # the submit counts toward the rate limit
        interaction.followup.send.assert_not_called()

    async def test_form_submit_refusals_and_a_failed_edit_reopen_the_form(self) -> None:
        cog = self._cog()
        channel = self._ticket_channel()
        interaction = self._submit(channel, user_id=7)
        with self._store(_ask_row(FORM)):
            await cog.on_interaction(interaction)
        interaction.response.send_message.assert_awaited_once_with(ai_asks.text("en", "not_yours"), ephemeral=True)

        interaction = self._submit(channel)
        with (
            self._store(_ask_row(FORM, language="es"), claimed=False),
            self._open_ticket(),
            patch("bulmaai.cogs.ai_tickets.ai_budget.is_paused", return_value=False),
        ):
            await cog.on_interaction(interaction)
        interaction.response.send_message.assert_awaited_once_with("Ya se respondió.", ephemeral=True)
        interaction.response.defer.assert_not_called()

        card = sent_message(self._card(FORM), message_id=55, channel_id=13)
        card.edit.side_effect = RuntimeError("boom")
        channel.fetch_message = AsyncMock(return_value=card)
        interaction = self._submit(channel)
        with (
            self._store(_ask_row(FORM)) as store,
            self._open_ticket(),
            patch("bulmaai.cogs.ai_tickets.ai_budget.is_paused", return_value=False),
            patch.object(cog, "_answer_support", AsyncMock()) as answer,
            self.assertLogs(ai_tickets.log, "ERROR"),
        ):
            await cog.on_interaction(interaction)
        store.release.assert_awaited_once_with(55)
        interaction.followup.send.assert_awaited_once_with(ai_asks.text("en", "failed"), ephemeral=True)
        answer.assert_not_called()

        interaction = self._submit(channel)
        interaction.data["custom_id"] = "some_other_modal"
        with self._store(_ask_row(FORM)) as store:
            await cog.on_interaction(interaction)
        store.get.assert_not_called()
        interaction.response.send_message.assert_not_called()

    async def test_the_reply_carries_the_controls_unless_it_hands_off(self) -> None:
        cog = self._cog()
        channel = self._ticket_channel()
        channel.created_at = datetime.now(timezone.utc)
        requester = types.SimpleNamespace(id=42, bot=False, name="goku", display_name="Goku")
        stand_in = ai_tickets._ChoiceMessage(types.SimpleNamespace(user=requester, channel=channel, guild=None), "Raza", 55)

        async def answer(result):
            with (
                patch.object(cog, "_extract_image_context", AsyncMock(return_value={})),
                patch.object(cog, "_build_history", AsyncMock(return_value=[])),
                patch.object(cog, "_ticket_language", AsyncMock(return_value="es")),
                patch("bulmaai.cogs.ai_tickets.run_support_agent", AsyncMock(return_value=result)),
                patch.object(cog, "_send_messages_with_typing", AsyncMock(return_value=True)) as send,
                patch.object(cog, "_awaiting_owner", AsyncMock()),
                patch.object(cog, "_mark_ticket_escalated", AsyncMock()),
                patch.object(cog, "_ping_escalation_roles", AsyncMock()),
                patch.object(cog, "_is_ticket_owner", AsyncMock(return_value=True)),
                patch.object(cog, "_maybe_prompt_resolution", AsyncMock()) as prompt,
            ):
                await cog._answer_support(stand_in, in_ticket=True, in_dm=False, mention_request=False, member=None)
            return send.await_args.kwargs, prompt

        base = {"tool_results": [], "confidence": 0.95, "suggested_close": False, "language": "es"}
        sent, prompt = await answer({**base, "reply": "¿Qué versión?", "kind": "answer", "ask": SELECT})
        self.assertEqual((sent["ask"], sent["requester_id"], sent["language"]), (SELECT, 42, "es"))
        prompt.assert_not_called()  # still asking, so no "solved?" prompt yet
        sent, prompt = await answer({**base, "reply": "Probá esto.", "kind": "answer", "ask": None})
        prompt.assert_awaited_once()
        sent, _ = await answer({**base, "reply": "El staff lo revisará.", "kind": "handoff", "ask": SELECT})
        self.assertIsNone(sent["ask"])

    async def test_a_public_reply_never_offers_an_upload(self) -> None:
        cog = self._cog()
        channel = self._ticket_channel()  # in_ticket=False below: answered as a public ping
        requester = types.SimpleNamespace(id=42, bot=False, name="goku", display_name="Goku")
        stand_in = ai_tickets._ChoiceMessage(types.SimpleNamespace(user=requester, channel=channel, guild=None), "Sí", 55)
        base = {"tool_results": [], "confidence": 0.9, "suggested_close": False, "language": "en", "kind": "clarify"}

        async def sent_ask(ask):
            with (
                patch.object(cog, "_extract_image_context", AsyncMock(return_value={})),
                patch.object(cog, "_build_history", AsyncMock(return_value=[])),
                patch("bulmaai.cogs.ai_tickets.run_support_agent", AsyncMock(return_value={**base, "reply": "Which?", "ask": ask})),
                patch("bulmaai.cogs.ai_tickets.can_post_publicly", return_value=True),
                patch.object(cog, "_send_messages_with_typing", AsyncMock(return_value=True)) as send,
            ):
                await cog._answer_support(stand_in, in_ticket=False, in_dm=False, mention_request=True, member=None)
            return send.await_args.kwargs["ask"]

        self.assertIsNone(await sent_ask(UPLOAD))
        self.assertEqual(await sent_ask(SELECT), SELECT)

    async def test_upload_opens_its_modal_without_claiming_or_spending(self) -> None:
        cog = self._cog()
        interaction = self._interaction(self._ticket_channel(), user_id=42, spec=UPLOAD, custom_id="ai_ask:file:42:0")
        with (
            self._store(_ask_row(UPLOAD, language="es")) as store,
            self._open_ticket(),
            patch("bulmaai.cogs.ai_tickets.ai_budget.is_paused", return_value=False),
        ):
            await cog.on_interaction(interaction)
        modal = interaction.response.send_modal.await_args.args[0]
        self.assertEqual((modal.custom_id, modal.title), ("ai_ask_file:55", "Sube tu log"))
        self.assertIsInstance(modal.children[0].item, discord.ui.FileUpload)
        store.claim.assert_not_called()
        self.assertEqual(dict(cog._support_events), {})

        interaction = self._interaction(self._ticket_channel(), user_id=7, spec=UPLOAD, custom_id="ai_ask:file:42:0")
        with self._store(_ask_row(UPLOAD)):
            await cog.on_interaction(interaction)
        interaction.response.send_message.assert_awaited_once_with(ai_asks.text("en", "not_yours"), ephemeral=True)
        interaction.response.send_modal.assert_not_called()

    async def test_where_is_it_answers_anyone_privately_and_claims_nothing(self) -> None:
        cog = self._cog()
        interaction = self._interaction(
            self._ticket_channel(), user_id=7, spec=UPLOAD, custom_id="ai_ask:file:42:1", values=["mac"]
        )
        with self._store(_ask_row(UPLOAD, language="es")) as store:
            await cog.on_interaction(interaction)
        call = interaction.response.send_message.await_args
        self.assertTrue(call.kwargs["ephemeral"])
        shown = "\n".join(texts(call.kwargs["view"]))
        self.assertIn("🍎 macOS (Cliente)", shown)
        self.assertIn("~/Library/Application Support/minecraft/logs/latest.log", shown)
        store.claim.assert_not_called()
        interaction.response.edit_message.assert_not_called()
        self.assertEqual(dict(cog._support_events), {})

        interaction = self._interaction(
            self._ticket_channel(), user_id=7, spec=UPLOAD, custom_id="ai_ask:file:42:1", values=["moon"]
        )
        with self._store(_ask_row(UPLOAD)):
            await cog.on_interaction(interaction)
        interaction.response.send_message.assert_awaited_once_with(ai_asks.text("en", "failed"), ephemeral=True)

    def _upload_submit(self, channel, user_id: int = 42, payloads=None, *, guild=None):
        payloads = payloads if payloads is not None else [
            {"id": "11", "filename": "latest.log", "size": 100, "url": "https://cdn/11", "content_type": "text/plain"},
            {"id": "12", "filename": "my shot.png", "size": 200, "url": "https://cdn/12", "content_type": "image/png"},
        ]
        return types.SimpleNamespace(
            type=discord.InteractionType.modal_submit,
            channel=channel,
            guild=guild,
            user=types.SimpleNamespace(id=user_id, mention=f"<@{user_id}>"),
            data={
                "custom_id": "ai_ask_file:55",
                "components": [
                    {"type": 18, "component": {"type": 19, "custom_id": "files", "values": [p["id"] for p in payloads]}}
                ],
                "resolved": {"attachments": {p["id"]: p for p in payloads}},
            },
            message=None,
            attachment_size_limit=None,
            _state=types.SimpleNamespace(http=types.SimpleNamespace(get_from_cdn=AsyncMock(return_value=b"file bytes"))),
            response=types.SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=types.SimpleNamespace(send=AsyncMock()),
        )

    def _upload_channel(self, order: list):
        channel = self._ticket_channel()
        card = sent_message(self._card(UPLOAD), message_id=55, channel_id=13)
        channel.fetch_message = AsyncMock(return_value=card)
        reposted = types.SimpleNamespace(
            id=60, guild=types.SimpleNamespace(id=1), attachments=[types.SimpleNamespace(id=61), types.SimpleNamespace(id=62)]
        )
        channel.send = AsyncMock(side_effect=lambda **kwargs: (order.append("repost"), reposted)[1])
        return channel, card, reposted

    async def test_upload_submit_reposts_records_analyzes_then_answers(self) -> None:
        cog = self._cog()
        order: list[str] = []
        channel, card, reposted = self._upload_channel(order)
        log_parser = types.SimpleNamespace(parse_upload=AsyncMock(side_effect=lambda *args: order.append("log card")))
        dmzdebug = types.SimpleNamespace(post_cards=AsyncMock(side_effect=lambda *args: order.append("dump card")))
        cog.bot.get_cog = {"LogParserCog": log_parser, "DmzDebugCog": dmzdebug}.get
        interaction = self._upload_submit(channel)
        with (
            self._store(_ask_row(UPLOAD)) as store,
            self._open_ticket(),
            patch("bulmaai.cogs.ai_tickets.ai_budget.is_paused", return_value=False),
            patch("bulmaai.cogs.ai_tickets.note_ticket_message", AsyncMock()) as noted,
            patch.object(cog, "_answer_support", AsyncMock(side_effect=lambda *a, **k: order.append("answer"))) as answer,
        ):
            await cog.on_interaction(interaction)
            await cog._pending_tasks[(13, 0)]

        store.claim.assert_awaited_once_with(55)
        interaction.response.defer.assert_awaited_once_with(invisible=True)
        self.assertEqual(order, ["repost", "log card", "dump card", "answer"])
        repost = channel.send.await_args.kwargs
        lines = ["-# ↳ <@42> uploaded **latest.log**", "-# ↳ <@42> uploaded **my_shot.png**"]
        self.assertEqual(texts(repost["view"]), lines)
        self.assertEqual([file.filename for file in repost["files"]], ["latest.log", "my_shot.png"])
        self.assertEqual(repost["files"][0].fp.read(), b"file bytes")
        (gallery,) = [item for item in walk(repost["view"]) if isinstance(item, discord.ui.MediaGallery)]
        self.assertEqual([item.url for item in gallery.items], ["attachment://my_shot.png"])
        self.assertEqual(repost["allowed_mentions"].to_dict()["parse"], [])
        interaction._state.http.get_from_cdn.assert_any_await("https://cdn/12")

        edit = card.edit.await_args.kwargs
        self.assertEqual(texts(edit["view"]), ["What do you want to change?", *lines])
        self.assertEqual([item for item in walk(edit["view"]) if isinstance(item, (discord.ui.Button, discord.ui.Select))], [])
        self.assertEqual(edit["allowed_mentions"].to_dict()["parse"], [])

        log_parser.parse_upload.assert_awaited_once_with(reposted, interaction.user)
        dmzdebug.post_cards.assert_awaited_once_with(reposted)
        noted.assert_awaited_once_with(13, 42)
        stand_in = answer.await_args.args[0]
        self.assertEqual(
            (stand_in.content, stand_in.id, stand_in.attachments),
            ("uploaded latest.log\nuploaded my_shot.png", 55, tuple(reposted.attachments)),
        )
        self.assertEqual(len(cog._support_events[(42, 0)]), 1)  # the submit counts toward the rate limit
        interaction.followup.send.assert_not_called()

    async def test_upload_refusals_and_a_failed_repost_reopen_the_question(self) -> None:
        cog = self._cog()
        cog.bot.get_cog = lambda name: None
        channel, card, _ = self._upload_channel([])

        @contextlib.contextmanager
        def submitted(interaction, row, *, claimed: bool = True):
            with (
                self._store(row, claimed=claimed) as store,
                self._open_ticket(),
                patch("bulmaai.cogs.ai_tickets.ai_budget.is_paused", return_value=False),
            ):
                yield store

        interaction = self._upload_submit(channel, user_id=7)
        with submitted(interaction, _ask_row(UPLOAD)) as store:
            await cog.on_interaction(interaction)
        interaction.response.send_message.assert_awaited_once_with(ai_asks.text("en", "not_yours"), ephemeral=True)
        store.claim.assert_not_called()

        # Over the guild's upload limit in total (100 + 200 bytes): nothing is claimed.
        interaction = self._upload_submit(channel, guild=types.SimpleNamespace(filesize_limit=250))
        with submitted(interaction, _ask_row(UPLOAD, language="es")) as store:
            await cog.on_interaction(interaction)
        interaction.response.send_message.assert_awaited_once_with(
            "Ese archivo es demasiado grande, comprímelo o escribe el error.", ephemeral=True
        )
        store.claim.assert_not_called()

        interaction = self._upload_submit(channel, payloads=[])
        with submitted(interaction, _ask_row(UPLOAD)) as store:
            await cog.on_interaction(interaction)
        interaction.response.send_message.assert_awaited_once_with(ai_asks.text("en", "failed"), ephemeral=True)
        store.claim.assert_not_called()

        interaction = self._upload_submit(channel)
        with submitted(interaction, _ask_row(UPLOAD), claimed=False):
            await cog.on_interaction(interaction)
        interaction.response.send_message.assert_awaited_once_with("Already answered.", ephemeral=True)
        interaction.response.defer.assert_not_called()

        channel.send = AsyncMock(side_effect=RuntimeError("upload failed"))
        interaction = self._upload_submit(channel)
        with (
            submitted(interaction, _ask_row(UPLOAD)) as store,
            patch.object(cog, "_answer_support", AsyncMock()) as answer,
            self.assertLogs(ai_tickets.log, "ERROR"),
        ):
            await cog.on_interaction(interaction)
        store.release.assert_awaited_once_with(55)
        interaction.followup.send.assert_awaited_once_with(ai_asks.text("en", "failed"), ephemeral=True)
        card.edit.assert_not_called()
        answer.assert_not_called()

    async def test_an_upload_stand_in_gets_its_screenshots_analyzed(self) -> None:
        cog = self._cog()
        shot = types.SimpleNamespace(id=62, filename="shot.png", content_type="image/png", url="https://cdn/62")
        log_file = types.SimpleNamespace(id=61, filename="latest.log", content_type="text/plain", url="https://cdn/61")
        interaction = types.SimpleNamespace(user=types.SimpleNamespace(id=42), channel=types.SimpleNamespace(id=13), guild=None)
        stand_in = ai_tickets._ChoiceMessage(interaction, "uploaded shot.png", 55, [log_file, shot])
        with (
            patch("bulmaai.cogs.ai_tickets.get_image_analyses", AsyncMock(return_value={})),
            patch("bulmaai.cogs.ai_tickets.save_image_analysis", AsyncMock()) as saved,
            patch.object(cog, "_extract_single_image_context", AsyncMock(return_value="A crash screen")) as vision,
        ):
            self.assertEqual(await cog._extract_image_context(stand_in), {62: "A crash screen"})
        vision.assert_awaited_once_with("https://cdn/62")
        saved.assert_awaited_once_with(attachment_id=62, channel_id=13, analysis="A crash screen")
        self.assertEqual(ai_tickets._ChoiceMessage(interaction, "Stats", 55).attachments, ())

    async def test_history_and_transcript_read_the_repost_as_the_uploaders_turn(self) -> None:
        cog = self._cog()
        requester = types.SimpleNamespace(id=42, bot=False, name="goku", display_name="Goku")
        now = datetime.now(timezone.utc)
        log_text = "[19:07:39] [main/INFO] [cp.mo.mo.Launcher/MODLAUNCHER]: ModLauncher running: args [--fml.forgeVersion, 47.4.10]\n"
        question = types.SimpleNamespace(
            id=1, author=requester, clean_content="My game crashes", attachments=[], created_at=now, reference=None, guild=None
        )
        lines = [ai_asks.upload_line(42, "latest.log")]
        card = types.SimpleNamespace(
            id=55, author=self.bot_user, clean_content="", attachments=[], created_at=now, reference=None, guild=None,
            components=self._answered(lines).components, edited_at=now, mentions=[],
        )
        uploaded = types.SimpleNamespace(
            id=9101, filename="latest.log", content_type="text/plain", size=len(log_text),
            read=AsyncMock(return_value=log_text.encode()),
        )
        repost = types.SimpleNamespace(
            id=60, author=self.bot_user, clean_content="", attachments=[uploaded], created_at=now, reference=None,
            guild=None, components=sent_message(ai_asks.upload_card(42, ["latest.log"], [False])).components,
            edited_at=None, mentions=[],
        )

        async def history(**kwargs):
            for item in ([question, card, repost] if kwargs.get("oldest_first") else [repost, card, question]):
                yield item

        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 13
        channel.history = history
        with patch("bulmaai.cogs.ai_tickets.get_image_analyses", new_callable=AsyncMock, return_value={}):
            turns = await cog._build_ticket_history(types.SimpleNamespace(channel=channel, author=requester), {})
            cog.bot.get_cog = lambda name: None
            transcript, _ = await cog._collect_transcript(channel, 42)

        self.assertEqual(
            [(turn["role"], turn["speaker_kind"]) for turn in turns],
            [("user", "requester"), ("assistant", "assistant"), ("user", "requester")],
        )
        self.assertEqual(turns[1]["content"], "What do you want to change?")  # the card's record lines aren't a turn
        self.assertTrue(turns[2]["content"].startswith("[Log analysis of latest.log]\nEnvironment:"))
        self.assertIn("Forge 47.4.10", turns[2]["content"])
        self.assertEqual(turns[2]["speaker_id"], "42")
        self.assertEqual(
            [(line.speaker_kind, line.content) for line in transcript],
            [("requester", "My game crashes"), ("assistant", "What do you want to change?"), ("requester", "[Attachment] latest.log")],
        )

    # --- steps ---

    def _steps_at(self, progress: int):
        """The steps card as posted, then moved on to progress."""
        return sent_message(
            ai_tickets._steps_card(sent_message(self._card(STEPS)), _ask_row(STEPS), progress), message_id=55, channel_id=13
        )

    def _click(self, index: int, *, progress: int = 0, card_progress: int | None = None, user_id: int = 42, channel=None):
        interaction = self._interaction(
            channel or self._ticket_channel(), user_id, spec=STEPS, custom_id=f"ai_ask:steps:42:{index}"
        )
        interaction.message = self._steps_at(progress if card_progress is None else card_progress)
        return interaction

    async def test_a_steps_card_is_sent_with_its_list_and_room_for_it(self) -> None:
        cog = self._cog()
        channel = types.SimpleNamespace(id=13, send=AsyncMock(return_value=types.SimpleNamespace(id=2)))
        with patch.object(ai_tickets.ask_store, "save", AsyncMock()) as save:
            await cog._send_messages_with_typing(channel, ["word " * 700], ai_answer=True, ask=STEPS, requester_id=42)
        views = [call.kwargs["view"] for call in channel.send.await_args_list]
        self.assertEqual(len(views), 2)  # 3500 characters fit one plain card, not one that also carries the steps
        for view in views:
            self.assertLessEqual(sum(map(len, texts(view))) + 300, TEXT_LIMIT)
        last = views[-1]
        self.assertEqual(texts(last)[-1], ai_asks.steps_text(STEPS, 0))
        self.assertIn(ai_tickets.AI_FOOTER, texts(last)[0])
        self.assertEqual([b.custom_id for b in buttons(last)], ["ai_ask:steps:42:0", "ai_ask:steps:42:1"])
        save.assert_awaited_once_with(message_id=2, channel_id=13, requester_id=42, spec=STEPS, language="en")

    async def test_done_moves_the_card_on_without_the_ai(self) -> None:
        cog = self._cog()
        cog._escalated_ticket_channels.add(13)  # AI off: a step done still moves on, only the outcome needs the AI
        interaction = self._click(0)
        with (
            self._store(_ask_row(STEPS)) as store,
            self._open_ticket(),
            patch.object(ai_tickets.ask_store, "move", AsyncMock(return_value=True)) as move,
            patch.object(cog, "_answer_support", AsyncMock()) as answer,
        ):
            await cog.on_interaction(interaction)

        busy = interaction.response.edit_message.await_args.kwargs["view"]
        self.assertEqual([(b.label, b.disabled) for b in buttons(busy)], [(WORKING, True), ("Still broken", True)])
        move.assert_awaited_once_with(55, 0, 1)
        final = interaction.edit_original_response.await_args.kwargs
        self.assertEqual(texts(final["view"]), ["What do you want to change?", ai_asks.steps_text(STEPS, 1)])
        self.assertEqual([b.custom_id for b in buttons(final["view"])], ["ai_ask:steps:42:2", "ai_ask:steps:42:3"])
        self.assertEqual(final["allowed_mentions"].to_dict()["parse"], [])
        store.claim.assert_not_called()
        answer.assert_not_called()
        self.assertEqual((dict(cog._support_events), cog._pending_tasks, cog._working), ({}, {}, set()))
        interaction.followup.send.assert_not_called()

    async def test_a_failed_edit_rewinds_the_step(self) -> None:
        cog = self._cog()
        interaction = self._click(2, progress=1)
        interaction.edit_original_response = AsyncMock(side_effect=[RuntimeError("boom"), None])
        with (
            self._store(_ask_row(STEPS, progress=1)),
            self._open_ticket(),
            patch.object(ai_tickets.ask_store, "move", AsyncMock(return_value=True)) as move,
            self.assertLogs("bulmaai.ui.v2", "ERROR"),
        ):
            await cog.on_interaction(interaction)
        self.assertEqual([call.args for call in move.await_args_list], [(55, 1, 2), (55, 2, 1)])
        interaction.followup.send.assert_awaited_once_with(ai_asks.text("en", "failed"), ephemeral=True)

    async def test_a_click_on_an_outdated_card_brings_it_up_to_date(self) -> None:
        cog = self._cog()
        interaction = self._click(1, progress=2, card_progress=0)  # "Still broken" on step 1, but they're on step 3
        with (
            self._store(_ask_row(STEPS, progress=2)) as store,
            self._open_ticket(),
            patch("bulmaai.cogs.ai_tickets.ai_budget.is_paused", return_value=False),
            patch.object(ai_tickets.ask_store, "move", AsyncMock()) as move,
            patch.object(cog, "_answer_support", AsyncMock()) as answer,
        ):
            await cog.on_interaction(interaction)
        view = interaction.response.edit_message.await_args.kwargs["view"]
        self.assertEqual(texts(view), ["What do you want to change?", ai_asks.steps_text(STEPS, 2)])
        self.assertEqual([b.custom_id for b in buttons(view)], ["ai_ask:steps:42:4", "ai_ask:steps:42:5"])
        interaction.followup.send.assert_awaited_once_with("Already answered.", ephemeral=True)
        move.assert_not_called()
        store.claim.assert_not_called()
        answer.assert_not_called()

    async def _finish_steps(self, interaction, row):
        cog = self._cog()
        with (
            self._store(row) as store,
            self._open_ticket(),
            patch("bulmaai.cogs.ai_tickets.ai_budget.is_paused", return_value=False),
            patch("bulmaai.cogs.ai_tickets.note_ticket_message", AsyncMock()) as noted,
            patch.object(cog, "_answer_support", AsyncMock()) as answer,
        ):
            await cog.on_interaction(interaction)
            await cog._pending_tasks[(13, 0)]
        store.claim.assert_awaited_once_with(55)
        noted.assert_awaited_once_with(13, 42)
        self.assertEqual(len(cog._support_events[(42, 0)]), 1)  # the outcome runs the AI, so it counts
        final = interaction.edit_original_response.await_args.kwargs["view"]
        self.assertEqual(buttons(final), [])
        return texts(final), answer.await_args.args[0]

    async def test_still_broken_records_the_step_and_hands_back_to_the_ai(self) -> None:
        shown, stand_in = await self._finish_steps(self._click(3, progress=1), _ask_row(STEPS, progress=1))
        self.assertEqual(
            shown,
            [
                "What do you want to change?",
                ai_asks.steps_text(STEPS, 1, failed=1),
                "-# ↳ <@42>: **Step 2 didn't work:** Delete the config folder",
            ],
        )
        self.assertEqual((stand_in.content, stand_in.id), ("Step 2 didn't work: Delete the config folder", 55))

    async def test_done_on_the_last_step_records_all_of_them(self) -> None:
        shown, stand_in = await self._finish_steps(self._click(4, progress=2), _ask_row(STEPS, language="es", progress=2))
        self.assertEqual(shown[1:], [ai_asks.steps_text(STEPS, 3), "-# ↳ <@42>: **Hice todos los pasos (1-3)**"])
        self.assertEqual(stand_in.content, "I did all the steps (1-3).")

    async def test_step_clicks_are_the_requesters_and_the_outcome_passes_the_ai_gates(self) -> None:
        cog = self._cog()
        interaction = self._click(0, user_id=7)
        with self._store(_ask_row(STEPS, language="pt")) as store:
            await cog.on_interaction(interaction)
        interaction.response.send_message.assert_awaited_once_with(ai_asks.text("pt", "not_yours"), ephemeral=True)

        cog._escalated_ticket_channels.add(13)
        interaction = self._click(1)
        with self._store(_ask_row(STEPS)) as store, self._open_ticket():
            await cog.on_interaction(interaction)
        interaction.response.send_message.assert_awaited_once_with(ai_asks.text("en", "ai_off"), ephemeral=True)
        store.claim.assert_not_called()

        interaction = self._click(7)  # step 4 of 3
        with self._store(_ask_row(STEPS)):
            await cog.on_interaction(interaction)
        interaction.response.send_message.assert_awaited_once_with(ai_asks.text("en", "failed"), ephemeral=True)

    async def test_history_reads_the_steps_done_and_the_outcome_as_the_requesters_turn(self) -> None:
        cog = self._cog()
        requester = types.SimpleNamespace(id=42, bot=False, name="goku", display_name="Goku")
        now = datetime.now(timezone.utc)
        question = types.SimpleNamespace(
            id=1, author=requester, clean_content="It crashes", attachments=[], created_at=now, reference=None, guild=None
        )
        card = types.SimpleNamespace(
            id=55, author=self.bot_user, clean_content="", attachments=[], created_at=now, reference=None, guild=None,
            components=self._steps_at(2).components, edited_at=now, mentions=[],
        )

        async def history(**kwargs):
            for item in ([question, card] if kwargs.get("oldest_first") else [card, question]):
                yield item

        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 13
        channel.history = history
        with patch("bulmaai.cogs.ai_tickets.get_image_analyses", new_callable=AsyncMock, return_value={}):
            turns = await cog._build_ticket_history(types.SimpleNamespace(channel=channel, author=requester), {})
            cog.bot.get_cog = lambda name: None
            lines, _ = await cog._collect_transcript(channel, 42)
        steps = ai_asks.steps_text(STEPS, 2)
        self.assertEqual(
            [(turn["role"], turn["content"]) for turn in turns],
            [("user", "It crashes"), ("assistant", f"What do you want to change?\n{steps}"), ("user", "I did steps 1-2.")],
        )
        self.assertEqual(turns[-1]["speaker_kind"], "requester")
        self.assertEqual([line.content for line in lines][-1], "I did steps 1-2.")

        line = ai_asks.step_failed_line(42, 2, "Start the game", "es")
        card.components = self._answered([line]).components
        self.assertEqual(cog._choice_turn(card, 42)["content"], "Step 3 didn't work: Start the game")

    # --- actions ---

    def _offer(self, spec, *, index: int = 0, channel=None, user_id: int = 42):
        interaction = self._interaction(
            channel or self._ticket_channel(), user_id, spec=spec, custom_id=f"ai_ask:action:42:{index}"
        )
        interaction.message = sent_message(self._card(spec), message_id=55, channel_id=13)
        return interaction

    async def test_get_staff_escalates_like_the_no_button_and_records_it(self) -> None:
        cog = self._cog()
        channel = self._ticket_channel()
        interaction = self._offer(GET_STAFF, channel=channel)
        with (
            self._store(_ask_row(GET_STAFF, language="es")) as store,
            self._open_ticket(),
            patch.object(cog, "_ticket_language", AsyncMock(return_value="pt")),
            patch.object(cog, "_mark_ticket_escalated", AsyncMock()) as escalate,
            patch.object(cog, "_ping_escalation_roles", AsyncMock()) as ping,
            patch.object(cog, "_answer_support", AsyncMock()) as answer,
        ):
            await cog.on_interaction(interaction)
        store.claim.assert_awaited_once_with(55)
        escalate.assert_awaited_once_with(13)
        ping.assert_awaited_once_with(channel, 42)
        final = interaction.edit_original_response.await_args.kwargs
        self.assertEqual(
            texts(final["view"]),
            ["What do you want to change?", "-# ↳ <@42>: **Llamar al staff**", ai_tickets.RESOLVE_PROMPT_TEXT["pt"]["escalated"]],
        )
        self.assertEqual((buttons(final["view"]), final["allowed_mentions"].to_dict()["parse"]), ([], []))
        answer.assert_not_called()
        self.assertEqual(dict(cog._support_events), {})

        # Staff already have it (the AI is off): recorded, but nobody is pinged again.
        cog._escalated_ticket_channels.add(13)
        interaction = self._offer(GET_STAFF, channel=channel)
        with (
            self._store(_ask_row(GET_STAFF)) as store,
            self._open_ticket(),
            patch.object(cog, "_ticket_language", AsyncMock(return_value="en")),
            patch.object(cog, "_ping_escalation_roles", AsyncMock()) as ping,
        ):
            await cog.on_interaction(interaction)
        store.claim.assert_awaited_once_with(55)
        ping.assert_not_called()
        self.assertIn("-# ↳ <@42>: **Get a staff member**", texts(interaction.edit_original_response.await_args.kwargs["view"]))

    async def test_actions_are_the_requesters_and_only_run_where_they_belong(self) -> None:
        cog = self._cog()
        interaction = self._offer(GET_STAFF, user_id=7)
        with self._store(_ask_row(GET_STAFF)) as store:
            await cog.on_interaction(interaction)
        interaction.response.send_message.assert_awaited_once_with(ai_asks.text("en", "not_yours"), ephemeral=True)

        interaction = self._offer(GET_STAFF)
        closed = types.SimpleNamespace(status="closed")
        with self._store(_ask_row(GET_STAFF)) as store, patch("bulmaai.cogs.ai_tickets.get_ticket_by_channel", AsyncMock(return_value=closed)):
            await cog.on_interaction(interaction)
        interaction.response.send_message.assert_awaited_once_with(ai_asks.text("en", "closed"), ephemeral=True)

        interaction = self._offer(GET_STAFF, channel=MagicMock(spec=discord.DMChannel))  # not a ticket
        with self._store(_ask_row(GET_STAFF)) as store:
            await cog.on_interaction(interaction)
        interaction.response.send_message.assert_awaited_once_with(ai_asks.text("en", "failed"), ephemeral=True)
        store.claim.assert_not_called()

    async def test_no_thanks_only_takes_the_offer_off(self) -> None:
        spec = {"kind": "action", "action": "link_patreon", "no_thanks": True}
        cog = self._cog()
        flow = types.SimpleNamespace(send_link_prompt=AsyncMock())
        cog.bot.get_cog = lambda name: flow
        interaction = self._offer(spec, index=1)
        with self._store(_ask_row(spec, language="es")) as store, self._open_ticket():
            await cog.on_interaction(interaction)
        store.claim.assert_awaited_once_with(55)
        final = interaction.edit_original_response.await_args.kwargs["view"]
        self.assertEqual((texts(final), buttons(final)), (["What do you want to change?", "-# ↳ <@42>: **No, gracias**"], []))
        flow.send_link_prompt.assert_not_called()

    async def test_link_patreon_sends_what_the_command_sends(self) -> None:
        spec = {"kind": "action", "action": "link_patreon", "label": "Link my Patreon"}
        cog = self._cog()
        flow = types.SimpleNamespace(send_link_prompt=AsyncMock())
        cog.bot.get_cog = lambda name: flow if name == "PatreonWhitelistFlowCog" else None
        interaction = self._offer(spec, channel=MagicMock(spec=discord.DMChannel))
        with self._store(_ask_row(spec)) as store:
            await cog.on_interaction(interaction)
        store.claim.assert_awaited_once_with(55)
        flow.send_link_prompt.assert_awaited_once_with(interaction.user, interaction.followup)
        self.assertEqual(texts(interaction.edit_original_response.await_args.kwargs["view"])[-1], "-# ↳ <@42>: **Link my Patreon**")

        flow.send_link_prompt = AsyncMock(side_effect=RuntimeError("boom"))
        interaction = self._offer(spec, channel=MagicMock(spec=discord.DMChannel))
        interaction.edit_original_response = AsyncMock()
        with self._store(_ask_row(spec)) as store, self.assertLogs("bulmaai.ui.v2", "ERROR"):
            await cog.on_interaction(interaction)
        store.release.assert_awaited_once_with(55)  # the offer stays open
        interaction.followup.send.assert_awaited_once_with(ai_asks.text("en", "failed"), ephemeral=True)

    def _public_channel(self) -> MagicMock:
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 13
        channel.category = types.SimpleNamespace(id=5)  # not the tickets category
        return channel

    async def test_open_ticket_opens_the_panels_form_or_its_select(self) -> None:
        cog = self._cog()
        cog.bot.get_cog = lambda name: object() if name == "TicketsCog" else None
        spec = {"kind": "action", "action": "open_ticket", "category": "bug"}

        @contextlib.contextmanager
        def offered(row, open_tickets=()):
            with (
                self._store(row) as store,
                patch("bulmaai.cogs.ai_tickets.get_ticket_by_channel", AsyncMock(return_value=None)),
                patch("bulmaai.cogs.ai_tickets.get_open_tickets_by_owner", AsyncMock(return_value=list(open_tickets))),
            ):
                yield store

        interaction = self._offer(spec, channel=self._public_channel())
        with offered(_ask_row(spec, language="es")) as store:
            await cog.on_interaction(interaction)
        store.claim.assert_awaited_once_with(55)
        modal = interaction.response.send_modal.await_args.args[0]
        self.assertIsInstance(modal, ticket_views.TicketModal)
        self.assertEqual(modal.category.key, "bug")
        edit = interaction.message.edit.await_args.kwargs
        self.assertEqual(texts(edit["view"])[-1], "-# ↳ <@42>: **Abrir un ticket**")
        self.assertEqual((buttons(edit["view"]), edit["allowed_mentions"].to_dict()["parse"]), ([], []))

        no_category = {"kind": "action", "action": "open_ticket"}
        interaction = self._offer(no_category, channel=self._public_channel())
        with offered(_ask_row(no_category)):
            await cog.on_interaction(interaction)
        call = interaction.response.send_message.await_args
        self.assertTrue(call.kwargs["ephemeral"])
        (select,) = [item for item in walk(call.kwargs["view"]) if isinstance(item, discord.ui.Select)]
        self.assertEqual(select.custom_id, ticket_views.PANEL_SELECT_ID)  # routed by TicketsCog like the panel's
        interaction.response.send_modal.assert_not_called()

        interaction = self._offer(spec, channel=self._public_channel())
        with offered(_ask_row(spec), open_tickets=[types.SimpleNamespace(channel_id=77)]) as store:
            await cog.on_interaction(interaction)
        interaction.response.send_message.assert_awaited_once_with(ticket_views.msg_already_open(77), ephemeral=True)
        store.claim.assert_not_called()
        interaction.message.edit.assert_not_called()

        interaction = self._offer(spec)  # inside a ticket
        with self._store(_ask_row(spec)) as store, self._open_ticket():
            await cog.on_interaction(interaction)
        interaction.response.send_message.assert_awaited_once_with(ai_asks.text("en", "failed"), ephemeral=True)
        store.claim.assert_not_called()

    async def test_replies_carry_an_action_only_where_it_runs(self) -> None:
        cog = self._cog()
        requester = types.SimpleNamespace(id=42, bot=False, name="goku", display_name="Goku")
        base = {"tool_results": [], "confidence": 0.9, "suggested_close": False, "language": "en", "kind": "answer"}

        async def sent_ask(ask, *, in_ticket: bool):
            channel = self._ticket_channel() if in_ticket else self._public_channel()
            channel.created_at = datetime.now(timezone.utc)
            stand_in = ai_tickets._ChoiceMessage(types.SimpleNamespace(user=requester, channel=channel, guild=None), "Hi", 55)
            with (
                patch.object(cog, "_extract_image_context", AsyncMock(return_value={})),
                patch.object(cog, "_build_history", AsyncMock(return_value=[])),
                patch.object(cog, "_ticket_language", AsyncMock(return_value="en")),
                patch("bulmaai.cogs.ai_tickets.run_support_agent", AsyncMock(return_value={**base, "reply": "Try this.", "ask": ask})) as run,
                patch("bulmaai.cogs.ai_tickets.can_post_publicly", return_value=True),
                patch.object(cog, "_send_messages_with_typing", AsyncMock(return_value=True)) as send,
                patch.object(cog, "_awaiting_owner", AsyncMock()),
                patch.object(cog, "_is_ticket_owner", AsyncMock(return_value=False)),
            ):
                await cog._answer_support(stand_in, in_ticket=in_ticket, in_dm=False, mention_request=not in_ticket, member=None)
            self.assertEqual(run.await_args.kwargs["channel_kind"], "ticket" if in_ticket else "public")
            return send.await_args.kwargs["ask"]

        open_ticket = {"kind": "action", "action": "open_ticket"}
        self.assertEqual(await sent_ask(GET_STAFF, in_ticket=True), GET_STAFF)
        self.assertIsNone(await sent_ask(GET_STAFF, in_ticket=False))
        self.assertIsNone(await sent_ask(open_ticket, in_ticket=True))
        self.assertEqual(await sent_ask(open_ticket, in_ticket=False), open_ticket)


if __name__ == "__main__":
    unittest.main()
