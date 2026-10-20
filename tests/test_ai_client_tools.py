import json
import os
import types
import unittest
from unittest.mock import AsyncMock, patch


os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.services import ai_budget
from bulmaai.services.ai_tools import ACTION_TOOL, ASK_TOOL, LOOKUP_TOOL_NAMES, SUPPORT_TOOL_NAMES
from bulmaai.services.ai_client import (
    CONTINUE_TURN,
    FALLBACK_BETA,
    REPEAT_NOTE,
    SEARCH_KNOWLEDGE_SCHEMA,
    START_TURN,
    _build_conversation,
    _build_trace_metadata,
    _create_message,
    _hydrate_tool_args,
    _is_repeat,
    _select_reasoning_effort,
    _split_confidence,
    _split_close,
    _split_kind,
    _append_source_links,
    get_schemas,
    run_support_agent,
)
from bulmaai.utils import tools_registry

HAIKU = "claude-haiku-5-5"
WIKI = "https://wiki.dragonminez.com/Transformations"
SONNET = "claude-sonnet-5-5"


class ConfidenceMarkerTests(unittest.TestCase):
    def test_strips_marker_and_parses_last_value(self) -> None:
        self.assertEqual(
            _split_confidence("Update GeckoLib.\n[confidence: 0.4]\n[Confidence=0.92]"),
            ("Update GeckoLib.", 0.92),
        )

    def test_clamps_and_handles_missing_marker(self) -> None:
        self.assertEqual(_split_confidence("Hi [confidence: 7]"), ("Hi", 1.0))
        self.assertEqual(_split_confidence("No marker."), ("No marker.", None))
        self.assertEqual(_split_confidence("[confidence: 0.1]"), ("(no reply)", 0.1))


class AIClientToolTests(unittest.IsolatedAsyncioTestCase):
    def test_selects_fast_effort_for_high_confidence_docs(self) -> None:
        settings = types.SimpleNamespace(ai_support_effort="medium", ai_support_fast_effort="low")
        self.assertEqual(_select_reasoning_effort(settings, high_confidence=True), "low")
        self.assertEqual(_select_reasoning_effort(settings, high_confidence=False), "medium")

    def test_tool_schemas_are_in_claude_format(self) -> None:
        schemas = get_schemas(SUPPORT_TOOL_NAMES)
        self.assertEqual([schema["name"] for schema in schemas], SUPPORT_TOOL_NAMES)
        self.assertEqual(SUPPORT_TOOL_NAMES, [*LOOKUP_TOOL_NAMES, ASK_TOOL, ACTION_TOOL])
        for schema in schemas[:-2]:
            self.assertEqual(set(schema), {"name", "description", "input_schema", "strict"})
            self.assertTrue(schema["strict"])
            input_schema = schema["input_schema"]
            self.assertEqual(input_schema["type"], "object")
            self.assertIs(input_schema["additionalProperties"], False)
            self.assertEqual(input_schema["required"], list(input_schema["properties"]))
        # ask_requester and offer_action aren't strict: optional fields, and every call is validated in code.
        ask, action = schemas[-2:]
        self.assertEqual(set(ask), {"name", "description", "input_schema"})
        self.assertEqual((ask["input_schema"]["required"], ask["input_schema"]["additionalProperties"]), (["kind"], False))
        self.assertEqual(ask["input_schema"]["properties"]["kind"]["enum"], ["choices", "select", "form", "file", "steps"])
        self.assertEqual(ask["input_schema"]["properties"]["what"]["enum"], ["log", "screenshot", "any"])
        self.assertEqual(set(action), {"name", "description", "input_schema"})
        properties = action["input_schema"]["properties"]
        self.assertEqual(
            (action["input_schema"]["required"], set(properties)),
            (["action"], {"action", "label", "category", "no_thanks", "title", "what_happened", "steps", "version"}),
        )
        self.assertEqual(properties["action"]["enum"], ["get_staff", "open_ticket", "link_patreon", "report_bug"])
        self.assertIn("report_bug (public channels and DMs, never in tickets)", action["description"])
        self.assertEqual(properties["category"]["enum"], ["bug", "contribute", "other"])
        self.assertIn("bug (Game-Breaking Bug: Crashes, corrupted worlds)", properties["category"]["description"])
        self.assertNotIn("channel_kind", properties)  # set from Discord, never by the model
        # The source schemas stay untouched.
        self.assertNotIn("required", tools_registry.TOOLS_SCHEMAS["get_patreon_status"]["parameters"])

    def test_search_knowledge_schema_is_strict_with_an_english_query(self) -> None:
        self.assertEqual(SEARCH_KNOWLEDGE_SCHEMA["name"], "search_knowledge")
        self.assertIn("English", SEARCH_KNOWLEDGE_SCHEMA["description"])
        self.assertEqual(
            SEARCH_KNOWLEDGE_SCHEMA["input_schema"],
            {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search keywords, in English."}},
                "required": ["query"],
                "additionalProperties": False,
            },
        )

    def test_builds_trace_metadata_as_strings(self) -> None:
        metadata = _build_trace_metadata(
            workflow="support_question",
            language="es",
            channel_id=456,
            user_id=123,
            file_search_enabled=True,
            ticket_conversation=True,
        )

        self.assertEqual(
            metadata,
            {
                "app": "dragonminez-ai",
                "workflow": "support_question",
                "language": "es",
                "discord_channel_id": "456",
                "discord_user": "discord-user-a665a45920422f9d",
                "file_search": "true",
                "ticket_conversation": "true",
            },
        )

    def test_no_function_tools_are_registered_for_patreon_whitelist(self) -> None:
        self.assertEqual(get_schemas(["start_patreon_whitelist_flow"]), [])

    def test_hydrate_forces_requester_identity_unless_staff_in_dm(self) -> None:
        other = {"discord_user_id": "555"}
        self.assertEqual(
            _hydrate_tool_args(name="get_patreon_status", args=other, user_id=123)["discord_user_id"], "123"
        )
        self.assertEqual(
            _hydrate_tool_args(
                name="get_patreon_status", args=other, user_id=123, requester_is_staff=True, channel_kind="ticket"
            )["discord_user_id"],
            "123",
        )
        self.assertEqual(
            _hydrate_tool_args(
                name="get_patreon_status", args=other, user_id=123, requester_is_staff=True, channel_kind="dm"
            )["discord_user_id"],
            "555",
        )
        self.assertEqual(
            _hydrate_tool_args(name="not_an_identity_tool", args={"include_patch_notes": True}, user_id=1),
            {"include_patch_notes": True},
        )

    def test_hydrate_tells_offer_action_where_it_is(self) -> None:
        args = {"action": "get_staff", "channel_kind": "ticket"}  # the model can't pick the channel
        self.assertEqual(
            _hydrate_tool_args(name=ACTION_TOOL, args=args, user_id=1, channel_kind="public"),
            {"action": "get_staff", "channel_kind": "public"},
        )
        self.assertNotIn("channel_kind", _hydrate_tool_args(name=ASK_TOOL, args={"kind": "file"}, user_id=1, channel_kind="dm"))

    def test_hydrate_forces_requester_for_known_issues(self) -> None:
        args = {"query": "x", "discord_user_id": "555"}
        for kwargs in ({}, {"requester_is_staff": True, "channel_kind": "ticket"}, {"channel_kind": "dm"}):
            self.assertEqual(
                _hydrate_tool_args(name="search_known_issues", args=args, user_id=123, **kwargs),
                {"query": "x", "discord_user_id": "123"},
            )
        self.assertEqual(
            _hydrate_tool_args(
                name="search_known_issues", args=args, user_id=123, requester_is_staff=True, channel_kind="dm"
            )["discord_user_id"],
            "555",
        )

    async def test_haiku_uses_plain_messages_and_other_models_the_server_fallback(self) -> None:
        fake = types.SimpleNamespace(
            messages=types.SimpleNamespace(create=AsyncMock(return_value="plain")),
            beta=types.SimpleNamespace(messages=types.SimpleNamespace(create=AsyncMock(return_value="beta"))),
        )
        with patch("bulmaai.services.ai_client.client", fake):
            self.assertEqual(await _create_message(timeout_seconds=1, model=HAIKU, max_tokens=10), "plain")
            self.assertEqual(await _create_message(timeout_seconds=1, model=SONNET, max_tokens=10), "beta")

        self.assertEqual(fake.messages.create.await_args.kwargs, {"model": HAIKU, "max_tokens": 10})
        self.assertEqual(
            fake.beta.messages.create.await_args.kwargs,
            {"model": SONNET, "max_tokens": 10, "betas": [FALLBACK_BETA], "fallbacks": "default"},
        )


class ConversationTests(unittest.TestCase):
    def test_turns_start_and_end_on_a_user_turn(self) -> None:
        context, turns = _build_conversation(
            [
                {"role": "assistant", "content": "Welcome! Describe your problem."},
                {"role": "user", "content": "It crashes", "speaker_id": "1", "speaker_kind": "requester"},
                {"role": "assistant", "content": "Send your log."},
            ],
            context_lines=["channel: support ticket"],
        )
        self.assertEqual(turns[0], {"role": "user", "content": START_TURN})
        self.assertEqual(turns[-1], {"role": "user", "content": CONTINUE_TURN})
        self.assertEqual([turn["role"] for turn in turns], ["user", "assistant", "user", "assistant", "user"])
        self.assertTrue(context.startswith("Context:\n- channel: support ticket\n"))
        self.assertIn("your earlier replies in this transcript: 2", context)

    def test_empty_conversation_still_has_a_user_turn(self) -> None:
        _, turns = _build_conversation([])
        self.assertEqual(turns, [{"role": "user", "content": START_TURN}])


class MarkerAndRepeatTests(unittest.TestCase):
    def test_splits_kind_marker(self) -> None:
        self.assertEqual(_split_kind("Hi\n[kind: Clarify]"), ("Hi", "clarify"))
        self.assertEqual(_split_kind("Hi"), ("Hi", None))
        self.assertEqual(_split_kind("[kind: offtopic]"), ("(no reply)", "offtopic"))

    def test_split_close_marker(self):
        self.assertEqual(_split_close("Done, the ticket can be closed.\n[close: Yes]"), ("Done, the ticket can be closed.", True))
        self.assertEqual(_split_close("Try reinstalling."), ("Try reinstalling.", False))

    def test_source_links_are_one_per_page_and_skip_non_urls(self) -> None:
        cited = [
            ("Forms: Super Saiyan (2/3)", "https://wiki.dragonminez.com/Forms#Super_Saiyan"),
            ("Forms: Kaioken", "https://wiki.dragonminez.com/Forms#Kaioken"),
            ("Ticket 12", "ticket-12.md"),
            ("Races", "https://wiki.dragonminez.com/Races"),
        ]
        self.assertEqual(
            _append_source_links("Hi", cited),
            "Hi\n-# 📖 [Forms: Super Saiyan](<https://wiki.dragonminez.com/Forms#Super_Saiyan>)"
            " · [Races](<https://wiki.dragonminez.com/Races>)",
        )
        self.assertEqual(_append_source_links("Hi", [("Ticket 12", "ticket-12.md")]), "Hi")
        self.assertEqual(_append_source_links("(no reply)", cited), "(no reply)")

    def test_detects_repeated_replies_but_not_short_or_different_ones(self) -> None:
        previous = ["Update GeckoLib to 4.8.3 and TerraBlender to 3.0.1.10, then restart the game."]
        self.assertTrue(
            _is_repeat("Update GeckoLib to 4.8.3 and TerraBlender to 3.0.1.10, then restart your game!", previous)
        )
        self.assertFalse(_is_repeat("Please send your latest.log so I can see the actual crash cause.", previous))
        self.assertFalse(_is_repeat("Ok!", ["Ok!"]))
        self.assertFalse(
            _is_repeat(previous[0] + "\n-# 📖 [Guide](<https://x>)", ["Something entirely different was said here."])
        )


def _settings(**overrides):
    values = dict(
        ai_support_model=HAIKU,
        ai_model=HAIKU,
        ai_support_escalation_model=SONNET,
        ai_support_escalation_confidence=0.7,
        ai_support_max_output_tokens=8000,
        ai_support_timeout_seconds=1,
        ai_support_effort="medium",
        ai_support_fast_effort="low",
        ai_knowledge_max_results=5,
    )
    values.update(overrides)
    return types.SimpleNamespace(**values)


def _text(text: str):
    return types.SimpleNamespace(type="text", text=text)


def _cited_text(text: str, title: str, source: str):
    citation = types.SimpleNamespace(type="search_result_location", title=title, source=source, cited_text=text)
    return types.SimpleNamespace(type="text", text=text, citations=[citation])


def _thinking():
    return types.SimpleNamespace(type="thinking", thinking="", signature="sig")


def _tool_use(name: str, call_id: str, tool_input=None):
    return types.SimpleNamespace(type="tool_use", id=call_id, name=name, input=tool_input or {})


def _message(
    text: str = "",
    *,
    response_id: str = "msg",
    content=None,
    stop_reason: str | None = None,
    model: str = HAIKU,
    category: str | None = None,
):
    """A fake Claude Message: 70 input + 10 output + 15 cache write + 5 cache read = 100 tokens."""
    blocks = content if content is not None else [_thinking(), _text(text)]
    if stop_reason is None:
        stop_reason = "tool_use" if any(block.type == "tool_use" for block in blocks) else "end_turn"
    return types.SimpleNamespace(
        id=response_id,
        model=model,
        content=blocks,
        stop_reason=stop_reason,
        stop_details=types.SimpleNamespace(category=category) if category else None,
        usage=types.SimpleNamespace(
            input_tokens=70,
            output_tokens=10,
            cache_creation_input_tokens=15,
            cache_read_input_tokens=5,
        ),
    )


class SupportAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        ai_budget.reset()
        self.messages = [
            {"role": "user", "content": "How do I transform?", "speaker_id": "123", "speaker_kind": "requester"},
        ]

    async def _run(
        self,
        responses,
        *,
        messages=None,
        knowledge=None,
        search_hits=None,
        workflow_prefix=None,
        channel_kind=None,
        **settings_overrides,
    ):
        with (
            patch(
                "bulmaai.services.ai_client._create_message",
                new_callable=AsyncMock,
                side_effect=list(responses),
            ) as create_message,
            patch(
                "bulmaai.services.ai_client._search_knowledge",
                new_callable=AsyncMock,
                return_value=knowledge or [],
            ),
            patch(
                "bulmaai.services.ai_client.knowledge.search",
                new_callable=AsyncMock,
                return_value=search_hits or [],
            ) as knowledge_search,
            patch("bulmaai.services.ai_client._support_info_text", new_callable=AsyncMock, return_value=""),
            patch("bulmaai.services.ai_client.record_support_ai_trace", new_callable=AsyncMock) as record_trace,
        ):
            result = await run_support_agent(
                messages=messages or self.messages,
                enabled_tools=SUPPORT_TOOL_NAMES,
                language_hint="en",
                user_id=123,
                channel_id=456,
                ticket_conversation=True,
                settings=_settings(**settings_overrides),
                context_lines=["channel: support ticket"],
                workflow_prefix=workflow_prefix,
                channel_kind=channel_kind,
            )
        self.knowledge_search = knowledge_search
        return result, create_message, record_trace

    async def test_fast_pass_offers_tools_at_fast_effort_and_is_grounded_in_prefetched_knowledge(self) -> None:
        result, create_message, record_trace = await self._run(
            [
                _message(
                    content=[
                        _thinking(),
                        _cited_text("Press G to transform.", "Transformations: Keys", WIKI + "#Keys"),
                        _text("\n[kind: answer]\n[confidence: 0.95]"),
                    ]
                )
            ],
            knowledge=[
                {"filename": "wiki--Transformations--keys.md", "title": "Transformations: Keys", "url": WIKI + "#Keys", "text": "Press G to transform."},
                {"filename": "dmz22-forms-saiyan.md", "title": "Saiyan forms", "url": None, "text": "SSJ needs..."},
            ],
        )

        create_message.assert_awaited_once()
        kwargs = create_message.await_args.kwargs
        self.assertEqual(kwargs["model"], HAIKU)
        self.assertEqual(kwargs["max_tokens"], 8000)
        self.assertEqual(kwargs["output_config"], {"effort": "low"})
        self.assertEqual(kwargs["metadata"], {"user_id": "discord-user-a665a45920422f9d"})
        for forbidden in ("thinking", "temperature", "top_p", "input", "instructions", "store", "reasoning"):
            self.assertNotIn(forbidden, kwargs)
        self.assertEqual(kwargs["tool_choice"], {"type": "auto"})
        tool_names = [tool["name"] for tool in kwargs["tools"]]
        self.assertEqual(tool_names, [*SUPPORT_TOOL_NAMES, "search_knowledge"])

        cached, dynamic = kwargs["system"]
        self.assertEqual(cached["cache_control"], {"type": "ephemeral"})
        self.assertNotIn("cache_control", dynamic)
        self.assertEqual(kwargs["cache_control"], {"type": "ephemeral"})
        self.assertIn("channel: support ticket", dynamic["text"])
        self.assertIn("Knowledge search results", dynamic["text"])
        self.assertNotIn("Press G", dynamic["text"])
        (turn,) = kwargs["messages"]
        self.assertEqual(turn["role"], "user")
        first, second, question = turn["content"]
        self.assertEqual(
            first,
            {
                "type": "search_result",
                "source": WIKI + "#Keys",
                "title": "Transformations: Keys",
                "content": [{"type": "text", "text": "Press G to transform."}],
                "citations": {"enabled": True},
            },
        )
        self.assertEqual((second["source"], second["citations"]), ("dmz22-forms-saiyan.md", {"enabled": True}))
        self.assertEqual(question, {"type": "text", "text": "[requester unknown]\nHow do I transform?"})

        self.assertEqual(result["reply"], f"Press G to transform.\n-# 📖 [Transformations: Keys](<{WIKI}#Keys>)")
        self.assertEqual((result["kind"], result["confidence"], result["escalated"]), ("answer", 0.95, False))
        trace = record_trace.await_args.args[0]
        self.assertEqual((trace.model, trace.workflow), (HAIKU, "support_question"))
        self.assertIn("search_knowledge", trace.tool_names)
        self.assertEqual(
            (trace.cached_tokens, trace.total_tokens, trace.reasoning_tokens, trace.previous_response_id),
            (5, 100, None, None),
        )
        self.assertEqual((trace.file_search_enabled, list(trace.vector_store_ids)), (True, []))
        self.assertEqual(trace.input_json[0]["role"], "system")
        self.assertEqual(trace.input_json[1:], kwargs["messages"])
        self.assertEqual(ai_budget.used(ai_budget.SMALL), 100)

    async def test_ticket_sends_the_whole_window_including_staff_and_earlier_replies(self) -> None:
        messages = [
            {"role": "user", "content": "Game crashes on start", "speaker_id": "123", "speaker_kind": "requester"},
            {"role": "assistant", "content": "Update GeckoLib."},
            {"role": "assistant", "content": "Then restart."},
            {"role": "user", "content": "Also check Java 17", "speaker_id": "7", "speaker_kind": "staff", "age": "2m ago"},
            {"role": "user", "content": "still broken", "speaker_id": "123", "speaker_kind": "requester"},
        ]
        _, create_message, _ = await self._run(
            [_message("Send your latest.log please.\n[kind: clarify]\n[confidence: 0.8]")], messages=messages
        )

        kwargs = create_message.await_args.kwargs
        turns = kwargs["messages"]
        rendered = "\n".join(turn["content"] for turn in turns)
        self.assertIn("Game crashes on start", rendered)
        self.assertIn("[staff unknown · 2m ago]\nAlso check Java 17", rendered)
        self.assertIn({"role": "assistant", "content": "Update GeckoLib.\nThen restart."}, turns)
        self.assertEqual(turns[-1]["role"], "user")
        self.assertIn("your earlier replies in this transcript: 1", kwargs["system"][1]["text"])

    async def test_low_confidence_answer_escalates_to_big_model_with_tools(self) -> None:
        result, create_message, record_trace = await self._run(
            [
                _message("Maybe try reinstalling.\n[kind: answer]\n[confidence: 0.3]"),
                _message(
                    "Delete config/dragonminez and restart.\n[kind: answer]\n[confidence: 0.9]",
                    response_id="r2",
                    model=SONNET,
                ),
            ]
        )

        self.assertEqual(create_message.await_count, 2)
        second = create_message.await_args_list[1].kwargs
        self.assertEqual((second["model"], second["output_config"]), (SONNET, {"effort": "medium"}))
        tool_names = [tool["name"] for tool in second["tools"]]
        self.assertIn("search_knowledge", tool_names)
        self.assertIn("get_patreon_status", tool_names)
        self.assertEqual(result["reply"], "Delete config/dragonminez and restart.")
        self.assertTrue(result["escalated"])
        self.assertEqual(result["response_id"], "r2")
        self.assertEqual(
            [call.args[0].workflow for call in record_trace.await_args_list],
            ["support_draft", "support_escalation"],
        )
        self.assertEqual((ai_budget.used(ai_budget.SMALL), ai_budget.used(ai_budget.BIG)), (100, 100))

    async def test_workflow_prefix_renames_every_trace(self) -> None:
        _, _, record_trace = await self._run(
            [_message("Update GeckoLib.\n[kind: answer]\n[confidence: 0.95]")], workflow_prefix="log_diagnosis"
        )
        self.assertEqual([call.args[0].workflow for call in record_trace.await_args_list], ["log_diagnosis"])

        _, _, record_trace = await self._run(
            [
                _message("Maybe reinstall.\n[kind: answer]\n[confidence: 0.3]"),
                _message("Update GeckoLib.\n[kind: answer]\n[confidence: 0.9]", response_id="r2", model=SONNET),
            ],
            workflow_prefix="log_diagnosis",
        )
        self.assertEqual(
            [call.args[0].workflow for call in record_trace.await_args_list],
            ["log_diagnosis_draft", "log_diagnosis_escalation"],
        )

    async def test_handoff_survives_an_escalation_reply_without_a_kind_marker(self) -> None:
        result, create_message, _ = await self._run(
            [
                _message("I'll pass this to staff.\n[kind: handoff]\n[confidence: 0.2]"),
                _message("Alright, I'll pass this to staff.", response_id="r2"),
            ]
        )

        self.assertEqual(create_message.await_count, 2)
        self.assertEqual((result["kind"], result["escalated"]), ("handoff", True))

    async def test_confident_clarify_and_missing_markers_do_not_escalate(self) -> None:
        for text in ("Which Minecraft version?\n[kind: clarify]\n[confidence: 0.4]", "Plain answer without markers."):
            _, create_message, _ = await self._run([_message(text)])
            create_message.assert_awaited_once()

    async def test_ask_requester_is_checked_locally_and_the_last_valid_call_goes_with_the_reply(self) -> None:
        tool_turn = _message(
            response_id="r_ask",
            content=[
                _thinking(),
                _tool_use(ASK_TOOL, "toolu_a", {"kind": "choices", "choices": ["latest.log", "<@1>", "crash report"]}),
                _tool_use(ASK_TOOL, "toolu_b", {"kind": "select", "options": [{"label": "1.20.1"}, {"label": "1.21"}]}),
                _tool_use(ASK_TOOL, "toolu_c", {"kind": "choices", "choices": ["only one"]}),
            ],
        )
        result, create_message, _ = await self._run(
            [tool_turn, _message("Which version do you play?\n[kind: clarify]\n[confidence: 0.9]", response_id="r_final")]
        )

        self.assertEqual(create_message.await_count, 2)
        results_turn = create_message.await_args_list[1].kwargs["messages"][-1]
        outputs = {block["tool_use_id"]: json.loads(block["content"]) for block in results_turn["content"]}
        self.assertEqual(outputs["toolu_a"]["shown"], {"kind": "choices", "choices": ["latest.log", "crash report"]})
        self.assertEqual(len(outputs["toolu_a"]["dropped"]), 1)  # the ping
        self.assertIn("write the reply", outputs["toolu_b"]["note"])
        self.assertEqual(outputs["toolu_c"]["ok"], False)  # the model can fix it; an invalid last call never wins
        self.assertFalse(any(block.get("is_error") for block in results_turn["content"]))
        self.assertEqual(result["reply"], "Which version do you play?")
        self.assertEqual(result["ask"], {"kind": "select", "options": [{"label": "1.20.1"}, {"label": "1.21"}]})
        self.assertFalse(result["escalated"])

    async def test_offer_action_is_checked_where_the_reply_goes_and_shares_the_last_call_rule(self) -> None:
        tool_turn = _message(
            response_id="r_offer",
            content=[
                _thinking(),
                _tool_use(ASK_TOOL, "toolu_a", {"kind": "steps", "steps": ["Close the game", "Delete config/dmz"]}),
                _tool_use(ACTION_TOOL, "toolu_b", {"action": "open_ticket", "channel_kind": "public"}),
                _tool_use(ACTION_TOOL, "toolu_c", {"action": "get_staff", "label": "Call a human", "no_thanks": True}),
            ],
        )
        result, create_message, _ = await self._run(
            [tool_turn, _message("Try deleting the config.\n[kind: answer]\n[confidence: 0.9]", response_id="r_final")],
            channel_kind="ticket",
        )
        outputs = {
            block["tool_use_id"]: json.loads(block["content"])
            for block in create_message.await_args_list[1].kwargs["messages"][-1]["content"]
        }
        self.assertEqual(outputs["toolu_a"]["shown"]["kind"], "steps")
        self.assertIn("only be offered in public channels", outputs["toolu_b"]["error"])  # this is a ticket
        self.assertEqual(result["tool_results"][1]["arguments"]["channel_kind"], "ticket")
        self.assertEqual(
            result["ask"], {"kind": "action", "action": "get_staff", "label": "Call a human", "no_thanks": True}
        )

    async def test_controls_go_only_with_a_sent_reply_that_is_not_a_handoff(self) -> None:
        ask = _tool_use(ASK_TOOL, "toolu_a", {"kind": "choices", "choices": ["Yes", "No"]})
        no_escalation = {"ai_daily_big_token_limit": 0, "ai_support_fast_effort": "high"}
        result, _, _ = await self._run(
            [_message(content=[ask]), _message("Staff will take a look.\n[kind: handoff]\n[confidence: 0.9]")],
            **no_escalation,
        )
        self.assertEqual((result["kind"], result["ask"]), ("handoff", None))

        result, _, _ = await self._run(
            [_message(content=[ask]), _message("", stop_reason="refusal")], **no_escalation
        )
        self.assertEqual((result["reply"], result["ask"]), ("(no reply)", None))

        result, _, _ = await self._run([_message("Plain answer.\n[kind: answer]\n[confidence: 0.95]")])
        self.assertIsNone(result["ask"])

    async def test_an_escalation_that_only_asked_keeps_the_first_answer(self) -> None:
        ask = _tool_use(ASK_TOOL, "toolu_a", {"kind": "choices", "choices": ["Yes", "No"]})
        result, create_message, _ = await self._run(
            [
                _message("Maybe reinstall.\n[kind: answer]\n[confidence: 0.3]"),
                _message(content=[ask], model=SONNET),
                _message("[kind: clarify]", response_id="r3", model=SONNET),
            ]
        )
        self.assertEqual(create_message.await_count, 3)
        self.assertEqual((result["reply"], result["ask"], result["escalated"]), ("Maybe reinstall.", None, False))

    async def test_second_clarify_in_a_row_escalates_and_hands_off_if_still_asking(self) -> None:
        messages = [
            {"role": "user", "content": "where are dev jars", "speaker_id": "123", "speaker_kind": "requester"},
            {"role": "assistant", "content": "Do you mean the patron channel?"},
            {"role": "user", "content": "YES", "speaker_id": "123", "speaker_kind": "requester"},
        ]
        result, create_message, _ = await self._run(
            [
                _message("Do you mean the dev builds channel?\n[kind: clarify]\n[confidence: 0.9]"),
                _message("Which channel exactly?\n[kind: clarify]\n[confidence: 0.9]", response_id="r2"),
            ],
            messages=messages,
        )

        self.assertEqual(create_message.await_count, 2)
        self.assertIn("Do not ask another one", create_message.await_args_list[1].kwargs["system"][1]["text"])
        self.assertEqual((result["kind"], result["escalated"]), ("handoff", True))

    async def test_escalation_falls_back_to_small_model_high_effort_when_big_pool_is_spent(self) -> None:
        result, create_message, _ = await self._run(
            [
                _message("Not sure.\n[kind: handoff]\n[confidence: 0.2]"),
                _message("Try /dmzrestoreupdate.\n[kind: answer]\n[confidence: 0.8]"),
            ],
            ai_daily_big_token_limit=0,
        )

        second = create_message.await_args_list[1].kwargs
        self.assertEqual((second["model"], second["output_config"]), (HAIKU, {"effort": "high"}))
        self.assertIn("tools", second)
        self.assertTrue(result["escalated"])

    async def test_no_escalation_when_the_route_is_the_fast_pass_again(self) -> None:
        _, create_message, _ = await self._run(
            [_message("Not sure.\n[kind: handoff]\n[confidence: 0.2]")],
            ai_daily_big_token_limit=0,
            ai_support_fast_effort="high",
        )
        create_message.assert_awaited_once()

    async def test_paused_when_small_pool_is_spent(self) -> None:
        result, create_message, _ = await self._run([], ai_daily_small_token_limit=0)

        create_message.assert_not_awaited()
        self.assertTrue(result["paused"])

    async def test_tool_loop_round_trips_tool_results_and_survives_tool_errors(self) -> None:
        async def broken_tool(**_kwargs):
            raise RuntimeError("db down")

        releases = AsyncMock(return_value={"latest_public_release": {"file": "dmz-2.1.1.jar"}})
        tool_turn = _message(
            response_id="r_tools",
            content=[
                _thinking(),
                _text("Let me check."),
                _tool_use("get_latest_releases", "toolu_1"),
                _tool_use("get_user_bug_reports", "toolu_2", {"discord_user_id": "555"}),
            ],
        )
        with patch.dict(
            tools_registry.TOOLS_FUNCS,
            {"get_latest_releases": releases, "get_user_bug_reports": broken_tool},
        ):
            result, create_message, record_trace = await self._run(
                [tool_turn, _message("Latest is 2.1.1.\n[kind: answer]\n[confidence: 0.9]", response_id="r_final")]
            )

        self.assertEqual(create_message.await_count, 2)
        followup = create_message.await_args_list[1].kwargs
        self.assertEqual(followup["tool_choice"], {"type": "auto"})
        assistant_turn, results_turn = followup["messages"][-2:]
        self.assertEqual(assistant_turn, {"role": "assistant", "content": tool_turn.content})
        self.assertEqual(results_turn["role"], "user")
        blocks = {block["tool_use_id"]: block for block in results_turn["content"]}
        self.assertEqual({block["type"] for block in blocks.values()}, {"tool_result"})
        self.assertEqual(
            json.loads(blocks["toolu_1"]["content"])["latest_public_release"]["file"], "dmz-2.1.1.jar"
        )
        self.assertNotIn("is_error", blocks["toolu_1"])
        self.assertEqual(json.loads(blocks["toolu_2"]["content"]), {"error": "tool_failed"})
        self.assertIs(blocks["toolu_2"]["is_error"], True)
        self.assertEqual(result["tool_results"][1]["arguments"], {"discord_user_id": "123"})
        self.assertEqual(releases.await_args.kwargs["discord_user_id"], "123")
        self.assertEqual(result["reply"], "Latest is 2.1.1.")
        self.assertFalse(result["escalated"])
        trace = record_trace.await_args.args[0]
        self.assertEqual((trace.response_id, trace.previous_response_id), ("r_final", None))
        self.assertEqual(trace.total_tokens, 200)
        self.assertIn("get_latest_releases", trace.tool_names)

    async def test_last_tool_round_forbids_more_tool_calls(self) -> None:
        releases = AsyncMock(return_value={})
        rounds = [
            _message(response_id=f"r{index}", content=[_tool_use("get_latest_releases", f"toolu_{index}")])
            for index in range(3)
        ]
        with patch.dict(tools_registry.TOOLS_FUNCS, {"get_latest_releases": releases}):
            result, create_message, _ = await self._run(
                [*rounds, _message("Done.\n[kind: answer]\n[confidence: 0.9]")]
            )

        self.assertEqual(create_message.await_count, 4)
        choices = [call.kwargs["tool_choice"] for call in create_message.await_args_list]
        self.assertEqual(choices, [{"type": "auto"}] * 3 + [{"type": "none"}])
        self.assertEqual(result["reply"], "Done.")

    async def test_suppressing_tool_ends_the_loop_without_a_reply_or_escalation(self) -> None:
        flow = AsyncMock(return_value={"suppress_ai_reply": True})
        with patch.dict(tools_registry.TOOLS_FUNCS, {"get_patreon_status": flow}):
            result, create_message, _ = await self._run(
                [_message(content=[_tool_use("get_patreon_status", "toolu_1")])]
            )

        create_message.assert_awaited_once()
        self.assertEqual((result["reply"], result["escalated"]), ("(no reply)", False))
        self.assertEqual(result["tool_results"][0]["output"], {"suppress_ai_reply": True})

    async def test_search_knowledge_hits_become_wiki_source_links(self) -> None:
        hit = {
            "filename": "wiki--Transformations.md",
            "title": "Transformations",
            "url": WIKI,
            "text": "Press G to transform.",
        }
        result, create_message, _ = await self._run(
            [
                _message(content=[_tool_use("search_knowledge", "toolu_k", {"query": "transform key"})]),
                _message(
                    content=[
                        _cited_text("Press G.", "Transformations", WIKI),
                        _cited_text(" Hold it to charge.", "Transformations", WIKI),
                        _text("\n[kind: answer]\n[confidence: 0.9]"),
                    ]
                ),
            ],
            search_hits=[hit],
        )

        self.knowledge_search.assert_awaited_once_with("transform key", 5)
        block = create_message.await_args_list[1].kwargs["messages"][-1]["content"][0]
        self.assertEqual(
            block["content"],
            [
                {
                    "type": "search_result",
                    "source": WIKI,
                    "title": "Transformations",
                    "content": [{"type": "text", "text": "Press G to transform."}],
                    "citations": {"enabled": True},
                }
            ],
        )
        self.assertEqual(
            result["tool_results"][0]["output"],
            {"results": [{"filename": "wiki--Transformations.md", "title": "Transformations"}]},
        )
        self.assertEqual(result["tool_results"][0]["arguments"], {"query": "transform key"})
        self.assertEqual(result["reply"], f"Press G. Hold it to charge.\n-# 📖 [Transformations](<{WIKI}>)")

    async def test_empty_knowledge_search_returns_a_text_block(self) -> None:
        _, create_message, _ = await self._run(
            [
                _message(content=[_tool_use("search_knowledge", "toolu_k", {"query": "zzz"})]),
                _message("Not sure.\n[kind: answer]\n[confidence: 0.9]"),
            ],
        )
        block = create_message.await_args_list[1].kwargs["messages"][-1]["content"][0]
        self.assertEqual(block["content"], [{"type": "text", "text": "No results found."}])

    async def test_failed_knowledge_search_is_reported_as_a_tool_error(self) -> None:
        with patch("bulmaai.services.ai_client.knowledge.search", AsyncMock(side_effect=OSError("db down"))):
            with patch("bulmaai.services.ai_client._create_message", new_callable=AsyncMock) as create_message:
                create_message.side_effect = [
                    _message(content=[_tool_use("search_knowledge", "toolu_k", {"query": "x"})]),
                    _message("Hm.\n[kind: answer]\n[confidence: 0.9]"),
                ]
                with (
                    patch("bulmaai.services.ai_client._support_info_text", new_callable=AsyncMock, return_value=""),
                    patch("bulmaai.services.ai_client.record_support_ai_trace", new_callable=AsyncMock),
                ):
                    await run_support_agent(
                        messages=self.messages,
                        enabled_tools=[],
                        language_hint="en",
                        user_id=123,
                        channel_id=456,
                        settings=_settings(),
                    )

        block = create_message.await_args_list[1].kwargs["messages"][-1]["content"][0]
        self.assertIs(block["is_error"], True)

    async def test_refusal_is_no_reply_and_escalates(self) -> None:
        with self.assertLogs("bulmaai.services.ai_client", level="INFO") as logs:
            result, create_message, record_trace = await self._run(
                [
                    _message("partial", stop_reason="refusal", category="cyber"),
                    _message("Here is how.\n[kind: answer]\n[confidence: 0.9]", response_id="r2", model=SONNET),
                ]
            )

        self.assertEqual(create_message.await_count, 2)
        self.assertEqual((result["reply"], result["escalated"]), ("Here is how.", True))
        self.assertTrue(any("refused (cyber)" in line and line.startswith("INFO") for line in logs.output))
        draft = record_trace.await_args_list[0].args[0]
        self.assertEqual((draft.reply_text, draft.request_metadata["stop_reason"]), ("(no reply)", "refusal"))

    async def test_refused_escalation_keeps_the_first_answer(self) -> None:
        result, _, _ = await self._run(
            [
                _message("Maybe reinstall.\n[kind: answer]\n[confidence: 0.3]"),
                _message("", stop_reason="refusal", model=SONNET),
            ]
        )
        self.assertEqual((result["reply"], result["escalated"]), ("Maybe reinstall.", False))

    async def test_max_tokens_is_logged_without_member_text(self) -> None:
        with self.assertLogs("bulmaai.services.ai_client", level="WARNING") as logs:
            await self._run([_message("Cut off answer [confidence: 0.9]", stop_reason="max_tokens")])
        line = next(line for line in logs.output if "max_tokens" in line)
        self.assertNotIn("transform", line)
        self.assertNotIn("Cut off", line)

    async def test_repeated_reply_escalates_with_note_and_hands_off_if_still_repeating(self) -> None:
        earlier = "Update GeckoLib to 4.8.3 and TerraBlender to 3.0.1.10, then restart the game."
        messages = [
            {"role": "user", "content": "It crashes", "speaker_id": "123", "speaker_kind": "requester"},
            {"role": "assistant", "content": earlier},
            {"role": "user", "content": "still crashes", "speaker_id": "123", "speaker_kind": "requester"},
        ]
        result, create_message, _ = await self._run(
            [
                _message(earlier + "\n[kind: answer]\n[confidence: 0.9]"),
                _message(earlier + "\n[kind: answer]\n[confidence: 0.9]"),
            ],
            messages=messages,
        )

        self.assertTrue(create_message.await_args_list[1].kwargs["system"][1]["text"].endswith(REPEAT_NOTE))
        self.assertNotIn(REPEAT_NOTE, create_message.await_args_list[0].kwargs["system"][1]["text"])
        self.assertEqual(result["kind"], "handoff")


if __name__ == "__main__":
    unittest.main()
