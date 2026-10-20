"""The "Ask Bulma" message command and the 👍/👎 feedback under Bulma's answers."""

import contextlib
import os
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.cogs import ai_tickets
from bulmaai.cogs import ask as ask_cog
from bulmaai.cogs.ai_tickets import AITicketsCog
from bulmaai.config import Settings
from bulmaai.services import ai_feedback as feedback_store
from bulmaai.ui import ai_asks, ai_feedback
from bulmaai.ui.v2 import WORKING
from v2_helpers import buttons, sent_message, texts

ACCESS_ROLE = 5
GUILD = 1
LOG_TEXT = "[19:07:39] [main/INFO] [cp.mo.mo.Launcher/MODLAUNCHER]: ModLauncher running: args [--fml.forgeVersion, 47.4.10]\n"
ANSWER_CARD_TEXT = f"Update Java to 17.\n\n{ai_tickets.AI_FOOTER}"


def _settings(**overrides):
    values = dict(
        ai_support_typing_lead_seconds=0,
        ai_support_enabled=True,
        ai_ticket_category_id=77,
        ticket_closed_category_id=None,
        ai_support_history_limit=10,
        discord_staff_role_ids=(),
        ai_support_allowed_role_ids=(ACCESS_ROLE,),
        panel_guild_id=GUILD,
        dev_guild_id=None,
        dev_jar_tester_role_ids=(),
        patreon_access_role_ids=(),
        ai_answer_feedback_enabled=True,
    )
    values.update(overrides)
    return types.SimpleNamespace(**values)


def _cog(**settings) -> AITicketsCog:
    bot_user = types.SimpleNamespace(id=999, bot=True, name="BulmaAI", display_name="BulmaAI")
    return AITicketsCog(types.SimpleNamespace(settings=_settings(**settings), user=bot_user, guilds=[]))


def _member(user_id: int = 42, *role_ids: int, name: str = "Goku") -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.bot = False
    member.name = member.display_name = name
    member.mention = f"<@{user_id}>"
    member.roles = [types.SimpleNamespace(id=0, name="@everyone"), *(types.SimpleNamespace(id=r, name=f"r{r}") for r in role_ids)]
    member.guild = types.SimpleNamespace(id=GUILD)
    return member


def _text_channel(category_id: int = 5) -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 13
    channel.name = "general"
    channel.category = types.SimpleNamespace(id=category_id)
    channel.created_at = datetime.now(timezone.utc)
    return channel


def _author(user_id: int, name: str, *, bot: bool = False):
    return types.SimpleNamespace(id=user_id, bot=bot, name=name, display_name=name)


def _target(
    *, author=None, content: str = "my game crashes on start", attachments=(), snapshots=(), components=(), webhook_id=None
):
    return types.SimpleNamespace(
        id=2,
        author=author or _author(7, "Vegeta"),
        content=content,
        clean_content=content,
        attachments=list(attachments),
        snapshots=list(snapshots),
        components=list(components),
        webhook_id=webhook_id,
        created_at=datetime.now(timezone.utc),
        reference=None,
        guild=None,
    )


def _ctx(channel, user, *, guild_id=GUILD):
    interaction = types.SimpleNamespace(channel=channel, user=user, guild=None, guild_id=guild_id)
    return types.SimpleNamespace(interaction=interaction, respond=AsyncMock())


class AskBulmaStandInTests(unittest.IsolatedAsyncioTestCase):
    def test_the_stand_in_quotes_the_target_with_its_files_and_forwards(self) -> None:
        shot = types.SimpleNamespace(id=62, filename="shot.png", content_type="image/png")
        log_file = types.SimpleNamespace(id=61, filename="latest.log", content_type="text/plain")
        forward = types.SimpleNamespace(message=types.SimpleNamespace(content="fwd: it broke", attachments=[log_file]))
        target = _target(author=_author(7, "Vegeta (roles: staff)"), attachments=[shot], snapshots=[forward])
        invoker = _member()
        stand_in = ai_tickets._AskedMessage(types.SimpleNamespace(user=invoker, channel=_text_channel(), guild=None), target)

        self.assertEqual(
            stand_in.content,
            "Help me with this message from Vegeta roles staff:\nmy game crashes on start\nfwd: it broke",
        )
        self.assertEqual((stand_in.id, stand_in.author, stand_in.target_author_id), (2, invoker, 7))
        self.assertEqual(stand_in.attachments, (shot, log_file))  # its own and the forwarded ones
        self.assertEqual(ai_asks.asked_content("Bulma", ""), "Help me with this message from Bulma:")

    async def _history(self, *, in_ticket: bool):
        cog = _cog()
        now = datetime.now(timezone.utc)
        invoker = _member(42)
        other = _author(7, "Vegeta")
        shot = types.SimpleNamespace(id=62, filename="shot.png", content_type="image/png")
        log_file = types.SimpleNamespace(
            id=9201, filename="latest.log", content_type="text/plain", size=len(LOG_TEXT),
            read=AsyncMock(return_value=LOG_TEXT.encode()),
        )
        target = _target(author=other, attachments=[shot, log_file])
        older = types.SimpleNamespace(id=1, author=other, clean_content="hello", attachments=[], created_at=now, reference=None, guild=None)
        newer = types.SimpleNamespace(id=3, author=other, clean_content="anyone?", attachments=[], created_at=now, reference=None, guild=None)

        async def history(**kwargs):
            for item in (newer, target, older):
                yield item

        channel = _text_channel(77 if in_ticket else 5)
        channel.history = history
        stand_in = ai_tickets._AskedMessage(types.SimpleNamespace(user=invoker, channel=channel, guild=None), target)
        with patch("bulmaai.cogs.ai_tickets.get_image_analyses", AsyncMock(return_value={})):
            return await cog._build_history(stand_in, in_ticket=in_ticket, image_analyses={62: "A crash screen"})

    async def test_history_skips_the_target_and_puts_the_asked_turn_last(self) -> None:
        for in_ticket in (False, True):
            turns = await self._history(in_ticket=in_ticket)
            self.assertEqual([turn["content"] for turn in turns[:2]], ["hello", "anyone?"])
            self.assertEqual(len(turns), 3)  # the target only once, inside the asked turn
            asked = turns[-1]
            self.assertEqual((asked["role"], asked["speaker_kind"], asked["speaker_id"]), ("user", "requester", "42"))
            self.assertTrue(asked["content"].startswith("Help me with this message from Vegeta:\nmy game crashes on start\n"))
            self.assertIn("[Image: A crash screen]", asked["content"])
            self.assertIn("[Log analysis of latest.log]", asked["content"])
            self.assertIn("Forge 47.4.10", asked["content"])

    async def test_context_lines_say_whose_message_was_asked_about(self) -> None:
        cog = _cog()
        channel = _text_channel()
        invoker = _member(42)
        interaction = types.SimpleNamespace(user=invoker, channel=channel, guild=None)

        def lines(author):
            stand_in = ai_tickets._AskedMessage(interaction, _target(author=author))
            return cog._support_context_lines(stand_in, member=invoker, in_ticket=False, in_dm=False)

        other = lines(_author(7, "Vegeta"))
        self.assertIn("they used Ask Bulma on a message", other[0])
        self.assertEqual(other[-1], ai_asks.asked_context_line(own_answer=False))
        self.assertEqual(lines(_author(999, "BulmaAI", bot=True))[-1], ai_asks.asked_context_line(own_answer=True))
        self.assertFalse(any(line.startswith("Ask Bulma") for line in lines(invoker)))  # their own message
        pick = ai_tickets._ChoiceMessage(interaction, "Stats", 55)
        self.assertIn("you were pinged", cog._support_context_lines(pick, member=invoker, in_ticket=False, in_dm=False)[0])


class AskBulmaCommandTests(unittest.IsolatedAsyncioTestCase):
    @contextlib.contextmanager
    def _env(self, cog, *, ticket=None, paused: bool = False):
        with (
            patch("bulmaai.cogs.ai_tickets.get_ticket_by_channel", AsyncMock(return_value=ticket)),
            patch("bulmaai.cogs.ai_tickets.ai_budget.is_paused", return_value=paused),
            patch.object(cog, "_answer_support", AsyncMock()) as answer,
        ):
            yield answer

    async def _run(self, cog, ctx, target, **env):
        with self._env(cog, **env) as answer:
            await AITicketsCog.ask_bulma.callback(cog, ctx, target)
            for task in list(cog._pending_tasks.values()):
                await task
        return answer

    def test_it_is_a_message_command_for_servers_and_bot_dms(self) -> None:
        command = AITicketsCog.ask_bulma
        self.assertIsInstance(command, discord.MessageCommand)
        self.assertEqual(command.name, "Ask Bulma")
        self.assertEqual(
            command.contexts, {discord.InteractionContextType.guild, discord.InteractionContextType.bot_dm}
        )

    async def test_on_it_then_the_answer_runs_in_the_background_as_a_ping(self) -> None:
        cog = _cog()
        invoker = _member(42, ACCESS_ROLE)
        ctx = _ctx(_text_channel(), invoker)
        target = _target()
        answer = await self._run(cog, ctx, target)

        ctx.respond.assert_awaited_once_with("On it…", ephemeral=True)
        stand_in = answer.await_args.args[0]
        self.assertIsInstance(stand_in, ai_tickets._AskedMessage)
        self.assertEqual((stand_in.id, stand_in.author, stand_in.content.splitlines()[0]), (2, invoker, "Help me with this message from Vegeta:"))
        self.assertEqual(
            answer.await_args.kwargs, {"in_ticket": False, "in_dm": False, "mention_request": True, "member": invoker}
        )
        self.assertEqual(len(cog._support_events[(42, 0)]), 1)  # it counts toward the rate limit
        self.assertEqual(cog._pending_tasks, {})

    async def test_in_a_ticket_it_answers_like_the_ticket_in_its_language(self) -> None:
        cog = _cog()
        staff_free_member = _member(42)  # no AI role needed inside a ticket
        ctx = _ctx(_text_channel(77), staff_free_member)
        ticket = types.SimpleNamespace(status="open", language="es")
        answer = await self._run(cog, ctx, _target(), ticket=ticket)
        ctx.respond.assert_awaited_once_with("Estoy en ello…", ephemeral=True)
        self.assertEqual(answer.await_args.kwargs["in_ticket"], True)
        self.assertEqual(answer.await_args.kwargs["mention_request"], False)

        cog._escalated_ticket_channels.add(13)  # AI off (staff took it)
        ctx = _ctx(_text_channel(77), staff_free_member)
        answer = await self._run(cog, ctx, _target(), ticket=ticket)
        ctx.respond.assert_awaited_once_with(ai_asks.text("es", "ask_off"), ephemeral=True)
        answer.assert_not_called()

    async def test_dms_work_for_members_with_support_access(self) -> None:
        cog = _cog()
        dm = MagicMock(spec=discord.DMChannel)
        dm.id = 31
        user = _author(42, "Goku")
        ctx = _ctx(dm, user, guild_id=None)
        with patch.object(cog, "_resolve_member_for_user", AsyncMock(return_value=_member(42, ACCESS_ROLE))):
            answer = await self._run(cog, ctx, _target(author=user))
        ctx.respond.assert_awaited_once_with("On it…", ephemeral=True)
        self.assertEqual(answer.await_args.kwargs["in_dm"], True)
        self.assertEqual(answer.await_args.kwargs["mention_request"], False)

        ctx = _ctx(dm, user, guild_id=None)
        with patch.object(cog, "_resolve_member_for_user", AsyncMock(return_value=None)):
            answer = await self._run(cog, ctx, _target(author=user))
        ctx.respond.assert_awaited_once_with(ai_asks.text("en", "ask_no_access"), ephemeral=True)
        answer.assert_not_called()

    async def test_refusals_are_ephemeral_and_start_nothing(self) -> None:
        bulma_card = sent_message(ai_tickets._card(ANSWER_CARD_TEXT))
        thread = MagicMock(spec=discord.Thread)
        thread.id = 13
        thread.type = discord.ChannelType.private_thread
        cases = [
            ("ask_off", dict(settings=dict(ai_support_enabled=False))),
            ("ask_off", dict(guild_id=555)),  # a server that isn't ours
            ("ask_off", dict(channel=thread)),  # nor are pings in private threads (tests/test_ai_threads: public ones)
            ("ask_bot", dict(target=_target(author=_author(8, "OtherBot", bot=True)))),
            ("ask_bot", dict(target=_target(author=_author(9, "Hook"), webhook_id=9))),
            ("ask_empty", dict(target=_target(content=""))),
            ("closed", dict(ticket=types.SimpleNamespace(status="closed", language="en"))),
            ("ask_no_access", dict(user=_member(42))),
            ("paused", dict(paused=True)),
        ]
        for key, case in cases:
            with self.subTest(key=key, case=case):
                cog = _cog(**case.get("settings", {}))
                ctx = _ctx(case.get("channel", _text_channel()), case.get("user", _member(42, ACCESS_ROLE)), guild_id=case.get("guild_id", GUILD))
                answer = await self._run(cog, ctx, case.get("target", _target()), ticket=case.get("ticket"), paused=case.get("paused", False))
                ctx.respond.assert_awaited_once_with(ai_asks.text("en", key), ephemeral=True)
                answer.assert_not_called()

        cog = _cog()  # Bulma's own card may be asked about
        target = _target(author=_author(999, "BulmaAI", bot=True), content="", components=bulma_card.components, webhook_id=999)
        answer = await self._run(cog, _ctx(_text_channel(), _member(42, ACCESS_ROLE)), target)
        self.assertEqual(answer.await_args.args[0].content, "Help me with this message from BulmaAI:\nUpdate Java to 17.")

    async def test_the_rate_limit_is_the_typed_paths(self) -> None:
        cog = _cog()
        invoker = _member(42, ACCESS_ROLE)
        for _ in range(ai_tickets.SUPPORT_RATE_LIMIT_CALLS):
            await self._run(cog, _ctx(_text_channel(), invoker), _target())
        ctx = _ctx(_text_channel(), invoker)
        answer = await self._run(cog, ctx, _target())
        ctx.respond.assert_awaited_once_with(ai_asks.text("en", "rate_limited"), ephemeral=True)
        answer.assert_not_called()


class FeedbackHelperTests(unittest.IsolatedAsyncioTestCase):
    def test_custom_ids_round_trip(self) -> None:
        self.assertEqual(ai_feedback.custom_id(42, True), "ai_fb:42:up")
        self.assertEqual(ai_feedback.parse_custom_id("ai_fb:42:down"), (42, False))
        for bad in ("ai_fb:42:maybe", "ai_fb:x:up", "ai_ask:choices:42:0", "ai_fb:42"):
            self.assertIsNone(ai_feedback.parse_custom_id(bad))

    async def test_the_row_is_two_emoji_buttons(self) -> None:
        row = ai_feedback.feedback_row(42)
        self.assertEqual(
            [(str(b.emoji), b.label, b.style, b.custom_id) for b in row.children],
            [
                ("👍", None, discord.ButtonStyle.secondary, "ai_fb:42:up"),
                ("👎", None, discord.ButtonStyle.secondary, "ai_fb:42:down"),
            ],
        )

    def test_which_answers_want_it_and_the_texts(self) -> None:
        on, off = _settings(), _settings(ai_answer_feedback_enabled=False)
        self.assertTrue(ai_feedback.wanted(on, "answer"))
        self.assertTrue(ai_feedback.wanted(on, None))
        for kind in ("clarify", "handoff", "offtopic"):
            self.assertFalse(ai_feedback.wanted(on, kind))
        self.assertFalse(ai_feedback.wanted(off, "answer"))
        self.assertIs(Settings.__dataclass_fields__["ai_answer_feedback_enabled"].default, True)  # on unless the panel says off
        self.assertEqual(ai_feedback.text("de", "thanks"), "-# Thanks for the feedback!")
        self.assertEqual(len(ai_feedback.THANKS_LINES), len(ai_feedback.TEXT))
        self.assertEqual({key for t in ai_feedback.TEXT.values() for key in t}, set(ai_feedback.TEXT["en"]))


class FeedbackPlacementTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_last_card_gets_the_row_and_its_row_is_saved(self) -> None:
        cog = _cog()
        channel = types.SimpleNamespace(id=13, send=AsyncMock(side_effect=[types.SimpleNamespace(id=1), types.SimpleNamespace(id=2)]))
        with patch.object(feedback_store, "save", AsyncMock()) as save:
            self.assertTrue(
                await cog._send_messages_with_typing(
                    channel, ["word " * 1000], ai_answer=True, requester_id=42, language="es", feedback=True, response_id="msg_1"
                )
            )
        first, last = (call.kwargs["view"] for call in channel.send.await_args_list)
        self.assertEqual(buttons(first), [])
        self.assertEqual([b.custom_id for b in buttons(last)], ["ai_fb:42:up", "ai_fb:42:down"])
        save.assert_awaited_once_with(message_id=2, channel_id=13, requester_id=42, response_id="msg_1", language="es")

    async def test_controls_win_and_a_failed_save_still_sends(self) -> None:
        cog = _cog()
        channel = types.SimpleNamespace(id=13, send=AsyncMock(return_value=types.SimpleNamespace(id=2)))
        choices = {"kind": "choices", "choices": ["Yes", "No"]}
        with patch.object(feedback_store, "save", AsyncMock()) as save, patch.object(ai_tickets.ask_store, "save", AsyncMock()):
            await cog._send_messages_with_typing(channel, ["Which?"], ai_answer=True, ask=choices, requester_id=42, feedback=True)
        self.assertEqual([b.custom_id for b in buttons(channel.send.await_args.kwargs["view"])], ["ai_ask:choices:42:0", "ai_ask:choices:42:1"])
        save.assert_not_called()

        with (
            patch.object(feedback_store, "save", AsyncMock(side_effect=RuntimeError("db down"))),
            self.assertLogs(ai_tickets.log, "ERROR"),
        ):
            self.assertTrue(await cog._send_messages_with_typing(channel, ["Do this."], ai_answer=True, requester_id=42, feedback=True))
        self.assertEqual(len(buttons(channel.send.await_args.kwargs["view"])), 2)

    async def _sent(self, cog, result, *, where: str = "public"):
        channel = _text_channel(77 if where == "ticket" else 5) if where != "dm" else MagicMock(spec=discord.DMChannel)
        channel.id = 13
        requester = _member(42)
        stand_in = ai_tickets._ChoiceMessage(types.SimpleNamespace(user=requester, channel=channel, guild=None), "Hi", 55)
        base = {"tool_results": [], "confidence": 0.9, "suggested_close": False, "language": "en", "response_id": "msg_7"}
        with (
            patch.object(cog, "_extract_image_context", AsyncMock(return_value={})),
            patch.object(cog, "_build_history", AsyncMock(return_value=[])),
            patch.object(cog, "_ticket_language", AsyncMock(return_value="en")),
            patch("bulmaai.cogs.ai_tickets.run_support_agent", AsyncMock(return_value={**base, **result})),
            patch.object(cog, "_send_messages_with_typing", AsyncMock(return_value=True)) as send,
            patch.object(cog, "_awaiting_owner", AsyncMock()),
            patch.object(cog, "_mark_ticket_escalated", AsyncMock()),
            patch.object(cog, "_ping_escalation_roles", AsyncMock()),
            patch.object(cog, "_is_ticket_owner", AsyncMock(return_value=False)),
        ):
            await cog._answer_support(
                stand_in, in_ticket=where == "ticket", in_dm=where == "dm", mention_request=where == "public", member=None
            )
        return send.await_args

    async def test_which_answers_get_the_buttons(self) -> None:
        cog = _cog()
        answer = {"reply": "Update Java.", "kind": "answer", "ask": None}
        sent = (await self._sent(cog, answer)).kwargs
        self.assertEqual((sent["feedback"], sent["response_id"], sent["requester_id"]), (True, "msg_7", 42))

        def feedback(result, **kwargs):
            return self._sent(cog, {**answer, **result}, **kwargs)

        self.assertTrue((await feedback({}, where="dm")).kwargs["feedback"])
        self.assertTrue((await feedback({"kind": None}, where="dm")).kwargs["feedback"])
        self.assertFalse((await feedback({}, where="ticket")).kwargs["feedback"])  # the "solved?" prompt's place
        self.assertFalse((await feedback({"kind": "clarify"})).kwargs["feedback"])
        self.assertFalse((await feedback({"kind": "handoff"}, where="dm")).kwargs["feedback"])
        self.assertFalse((await feedback({"ask": {"kind": "choices", "choices": ["A", "B"]}})).kwargs["feedback"])
        fallback = await feedback({"guard_flags": frozenset({"link"})})  # can't go public: our fallback text instead
        self.assertEqual((fallback.args[1], fallback.kwargs["feedback"]), ([ai_tickets.PUBLIC_FALLBACK_TEXT], False))
        self.assertFalse((await self._sent(_cog(ai_answer_feedback_enabled=False), answer)).kwargs["feedback"])


class FeedbackClickTests(unittest.IsolatedAsyncioTestCase):
    def _click(self, user_id: int = 42, custom_id: str = "ai_fb:42:down"):
        card = ai_tickets._card(ANSWER_CARD_TEXT, ai_tickets.AI_CARD_COLOR, ai_feedback.feedback_row(42))
        return types.SimpleNamespace(
            type=discord.InteractionType.component,
            channel=types.SimpleNamespace(id=13),
            user=types.SimpleNamespace(id=user_id),
            data={"custom_id": custom_id},
            message=sent_message(card, message_id=55, channel_id=13),
            response=types.SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
            edit_original_response=AsyncMock(),
            followup=types.SimpleNamespace(send=AsyncMock()),
        )

    @contextlib.contextmanager
    def _store(self, *, language: str = "es", rated: bool = True, row: bool = True):
        saved = feedback_store.FeedbackRow(55, 13, 42, "msg_1", None, language, datetime.now(timezone.utc))
        with (
            patch.object(feedback_store, "get", AsyncMock(return_value=saved if row else None)) as get,
            patch.object(feedback_store, "rate", AsyncMock(return_value=rated)) as rate,
        ):
            yield types.SimpleNamespace(get=get, rate=rate)

    async def test_the_requesters_click_is_stored_and_the_card_says_thanks(self) -> None:
        cog = _cog()
        interaction = self._click()
        with self._store() as store:
            await cog.on_interaction(interaction)

        busy = interaction.response.edit_message.await_args.kwargs["view"]
        self.assertEqual([(b.label, b.disabled) for b in buttons(busy)], [(None, True), (WORKING, True)])
        store.rate.assert_awaited_once_with(message_id=55, channel_id=13, requester_id=42, helpful=False)
        final = interaction.edit_original_response.await_args.kwargs
        self.assertEqual(buttons(final["view"]), [])
        self.assertEqual(texts(final["view"]), [ANSWER_CARD_TEXT, "-# ¡Gracias por tu opinión!"])
        self.assertEqual(final["allowed_mentions"].to_dict()["parse"], [])
        interaction.followup.send.assert_not_called()
        self.assertEqual(cog._working, set())

        # The thanks line never reaches the model or a transcript.
        rated = sent_message(final["view"])
        self.assertEqual(ai_tickets._card_text(rated), "Update Java to 17.")
        self.assertIsNone(ai_tickets._card_answer(rated))

    async def test_only_the_requester_rates_and_the_first_rating_wins(self) -> None:
        cog = _cog()
        interaction = self._click(user_id=7)
        with self._store(language="pt") as store:
            await cog.on_interaction(interaction)
        interaction.response.send_message.assert_awaited_once_with(ai_feedback.text("pt", "not_yours"), ephemeral=True)
        store.rate.assert_not_called()
        interaction.response.edit_message.assert_not_called()

        interaction = self._click(custom_id="ai_fb:42:up")
        with self._store(rated=False, row=False) as store:  # already rated (or never saved): the card still settles
            await cog.on_interaction(interaction)
        store.rate.assert_awaited_once_with(message_id=55, channel_id=13, requester_id=42, helpful=True)
        self.assertEqual(texts(interaction.edit_original_response.await_args.kwargs["view"])[-1], "-# Thanks for the feedback!")

    async def test_a_failed_lookup_or_edit_tells_the_clicker(self) -> None:
        cog = _cog()
        interaction = self._click()
        with (
            patch.object(feedback_store, "get", AsyncMock(side_effect=RuntimeError("db down"))),
            self.assertLogs(ai_tickets.log, "ERROR"),
        ):
            await cog.on_interaction(interaction)
        interaction.response.send_message.assert_awaited_once_with(ai_asks.text("en", "failed"), ephemeral=True)

        interaction = self._click()
        interaction.edit_original_response = AsyncMock(side_effect=[RuntimeError("boom"), None])
        with self._store(), self.assertLogs("bulmaai.ui.v2", "ERROR"):
            await cog.on_interaction(interaction)
        interaction.followup.send.assert_awaited_once_with(ai_asks.text("es", "failed"), ephemeral=True)
        self.assertEqual(len(buttons(interaction.edit_original_response.await_args.kwargs["view"])), 2)  # back as it was


class AskCommandFeedbackTests(unittest.IsolatedAsyncioTestCase):
    def _cog(self, **overrides):
        settings = _settings(
            ask_allowed_channel_ids=(13,), ask_rate_limit_window_seconds=60, ask_rate_limit_max_calls=5,
            discord_staff_role_ids=(3,), **overrides,
        )
        return ask_cog.AskCog(types.SimpleNamespace(settings=settings))

    async def _ask(self, cog, *, kind: str = "answer", reply: str = "Update Java.", flags: frozenset = frozenset()):
        author = _member(42, 3)
        followup_ids = iter(range(100, 110))
        ctx = types.SimpleNamespace(
            channel=types.SimpleNamespace(id=13, name="help"),
            author=author,
            guild=None,
            respond=AsyncMock(),
            defer=AsyncMock(),
            followup=types.SimpleNamespace(send=AsyncMock(side_effect=lambda *a, **k: types.SimpleNamespace(id=next(followup_ids)))),
        )
        result = {
            "reply": reply, "kind": kind, "language": "en", "response_id": "msg_9", "tool_results": [], "guard_flags": flags,
        }
        with (
            patch.object(ask_cog, "run_support_agent", AsyncMock(return_value=result)),
            patch.object(feedback_store, "save", AsyncMock()) as save,
        ):
            await ask_cog.AskCog.ask.callback(cog, ctx, "How do I fix my crash?")
        ctx.defer.assert_awaited_once_with()
        self.sends = ctx.followup.send.await_args_list
        return ctx.followup.send.await_args.kwargs["view"], save

    async def test_answers_get_the_buttons_unless_off(self) -> None:
        view, save = await self._ask(self._cog())
        self.assertEqual([b.custom_id for b in buttons(view)], ["ai_fb:42:up", "ai_fb:42:down"])
        save.assert_awaited_once_with(message_id=100, channel_id=13, requester_id=42, response_id="msg_9", language="en")

        view, save = await self._ask(self._cog(ai_answer_feedback_enabled=False))
        self.assertEqual(buttons(view), [])
        save.assert_not_called()

    async def test_every_reply_is_posted_publicly_as_written(self) -> None:
        for kind, flags in (("offtopic", frozenset()), ("handoff", frozenset()), ("answer", frozenset({"mention"}))):
            view, save = await self._ask(self._cog(), kind=kind, flags=flags)
            self.assertIn("Update Java.", "\n".join(texts(view)))
            self.assertEqual(len(self.sends), 1)
            self.assertNotIn("ephemeral", self.sends[0].kwargs)
            if kind != "answer":
                self.assertEqual(buttons(view), [])
                save.assert_not_called()

    async def test_links_that_arent_ours_go_out_unclickable(self) -> None:
        reply = "Get it at https://evil.example/mod and read https://wiki.dragonminez.com/Forms."
        view, save = await self._ask(self._cog(wiki_base_url="https://wiki.dragonminez.com"), reply=reply, flags=frozenset({"unknown_link"}))
        text = "\n".join(texts(view))
        self.assertIn("https:​//evil.example/mod", text)
        self.assertIn("https://wiki.dragonminez.com/Forms", text)
        save.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
