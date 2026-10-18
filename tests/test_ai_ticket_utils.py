import os
import types
import unittest
import asyncio
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
from bulmaai.services.ticket_intake import owner_says_solved
from bulmaai.ui.v2 import WORKING
from v2_helpers import sent_message
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


if __name__ == "__main__":
    unittest.main()
