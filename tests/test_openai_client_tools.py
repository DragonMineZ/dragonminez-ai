import json
import os
import types
import unittest
from unittest.mock import AsyncMock, patch


os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.services import ai_budget
from bulmaai.services.ai_tools import SUPPORT_TOOL_NAMES
from bulmaai.services.openai_client import (
    REPEAT_NOTE,
    _build_file_search_tool,
    _build_openai_metadata,
    _hydrate_tool_args,
    _is_repeat,
    _select_reasoning_effort,
    _split_confidence,
    _split_kind,
    _split_sources,
    get_schemas,
    run_support_agent,
)
from bulmaai.utils import tools_registry


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


class OpenAIClientToolTests(unittest.IsolatedAsyncioTestCase):
    def test_selects_fast_reasoning_for_high_confidence_docs(self) -> None:
        settings = types.SimpleNamespace(
            openai_support_reasoning_effort="medium",
            openai_support_fast_reasoning_effort="low",
        )

        self.assertEqual(
            _select_reasoning_effort(settings, high_confidence=True),
            "low",
        )

    def test_selects_default_reasoning_for_low_confidence_docs(self) -> None:
        settings = types.SimpleNamespace(
            openai_support_reasoning_effort="medium",
            openai_support_fast_reasoning_effort="low",
        )

        self.assertEqual(
            _select_reasoning_effort(settings, high_confidence=False),
            "medium",
        )

    def test_builds_openai_file_search_tool_from_vector_store_settings(self) -> None:
        settings = types.SimpleNamespace(
            openai_support_vector_store_ids=("vs_docs", "vs_tickets"),
            openai_support_file_search_max_results=6,
        )

        self.assertEqual(
            _build_file_search_tool(settings),
            {
                "type": "file_search",
                "vector_store_ids": ["vs_docs", "vs_tickets"],
                "max_num_results": 6,
            },
        )

    def test_builds_openai_dashboard_metadata_as_strings(self) -> None:
        metadata = _build_openai_metadata(
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
        self.assertEqual(_hydrate_tool_args(name="search_known_issues", args={"query": "x"}, user_id=1), {"query": "x"})


class MarkerAndRepeatTests(unittest.TestCase):
    def test_splits_kind_and_sources_markers(self) -> None:
        self.assertEqual(_split_kind("Hi\n[kind: Clarify]"), ("Hi", "clarify"))
        self.assertEqual(_split_kind("Hi"), ("Hi", None))
        self.assertEqual(_split_kind("[kind: offtopic]"), ("(no reply)", "offtopic"))
        self.assertEqual(
            _split_sources("Hi\n[sources: wiki--A.md, `wiki--B.md`]"),
            ("Hi", ["wiki--A.md", "wiki--B.md"]),
        )

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
        openai_support_model="gpt-5-mini",
        openai_model="gpt-5-mini",
        openai_support_escalation_model="gpt-5",
        ai_support_escalation_confidence=0.7,
        openai_support_max_output_tokens=100,
        ai_support_timeout_seconds=1,
        openai_support_reasoning_effort="medium",
        openai_support_fast_reasoning_effort="low",
        openai_support_vector_store_ids=("vs_docs",),
        openai_support_file_search_max_results=5,
        openai_support_store_responses=True,
    )
    values.update(overrides)
    return types.SimpleNamespace(**values)


def _response(text: str = "", *, response_id: str = "resp", output=None, total_tokens: int = 100):
    return types.SimpleNamespace(
        id=response_id,
        model="gpt-5-mini",
        output=output or [],
        output_text=text,
        usage=types.SimpleNamespace(
            input_tokens=total_tokens - 10,
            output_tokens=10,
            total_tokens=total_tokens,
            input_tokens_details=types.SimpleNamespace(cached_tokens=5),
            output_tokens_details=types.SimpleNamespace(reasoning_tokens=2),
        ),
    )


def _function_call(name: str, call_id: str, arguments: str = "{}"):
    return types.SimpleNamespace(type="function_call", name=name, call_id=call_id, arguments=arguments)


class SupportAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        ai_budget.reset()
        self.messages = [
            {"role": "user", "content": "How do I transform?", "speaker_id": "123", "speaker_kind": "requester"},
        ]

    async def _run(self, responses, *, messages=None, knowledge=None, **settings_overrides):
        with (
            patch(
                "bulmaai.services.openai_client._create_response",
                new_callable=AsyncMock,
                side_effect=list(responses),
            ) as create_response,
            patch(
                "bulmaai.services.openai_client._search_knowledge",
                new_callable=AsyncMock,
                return_value=knowledge or [],
            ),
            patch("bulmaai.services.openai_client.record_support_ai_trace", new_callable=AsyncMock) as record_trace,
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
            )
        return result, create_response, record_trace

    async def test_first_pass_is_tool_free_and_grounded_in_searched_knowledge(self) -> None:
        result, create_response, record_trace = await self._run(
            [_response("Press G to transform.\n[sources: wiki--Transformations.md]\n[kind: answer]\n[confidence: 0.95]")],
            knowledge=[{"filename": "wiki--Transformations.md", "text": "Press G to transform."}],
        )

        create_response.assert_awaited_once()
        kwargs = create_response.await_args.kwargs
        self.assertNotIn("tools", kwargs)
        self.assertNotIn("conversation", kwargs)
        self.assertEqual(kwargs["model"], "gpt-5-mini")
        self.assertEqual(kwargs["reasoning"]["effort"], "low")
        rendered = "\n".join(item["content"] for item in kwargs["input"])
        self.assertIn("channel: support ticket", rendered)
        self.assertIn("### wiki--Transformations.md\nPress G to transform.", rendered)
        self.assertIn("[requester unknown]\nHow do I transform?", rendered)
        self.assertTrue(result["reply"].startswith("Press G to transform.\n-# 📖 ["))
        self.assertEqual((result["kind"], result["confidence"], result["escalated"]), ("answer", 0.95, False))
        trace = record_trace.await_args.args[0]
        self.assertEqual((trace.model, trace.workflow, trace.tool_names), ("gpt-5-mini", "support_question", []))
        self.assertEqual(trace.cached_tokens, 5)
        self.assertEqual(ai_budget.used(ai_budget.SMALL), 100)

    async def test_ticket_sends_the_whole_window_including_staff_and_earlier_replies(self) -> None:
        messages = [
            {"role": "user", "content": "Game crashes on start", "speaker_id": "123", "speaker_kind": "requester"},
            {"role": "assistant", "content": "Update GeckoLib."},
            {"role": "assistant", "content": "Then restart."},
            {"role": "user", "content": "Also check Java 17", "speaker_id": "7", "speaker_kind": "staff", "age": "2m ago"},
            {"role": "user", "content": "still broken", "speaker_id": "123", "speaker_kind": "requester"},
        ]
        _, create_response, _ = await self._run(
            [_response("Send your latest.log please.\n[kind: clarify]\n[confidence: 0.8]")], messages=messages
        )

        request_input = create_response.await_args.kwargs["input"]
        rendered = "\n".join(item["content"] for item in request_input)
        self.assertIn("Game crashes on start", rendered)
        self.assertIn("[staff unknown · 2m ago]\nAlso check Java 17", rendered)
        self.assertIn({"role": "assistant", "content": "Update GeckoLib.\nThen restart."}, request_input)
        self.assertIn("your earlier replies in this transcript: 1", request_input[0]["content"])

    async def test_low_confidence_answer_escalates_to_big_model_with_tools(self) -> None:
        result, create_response, record_trace = await self._run(
            [
                _response("Maybe try reinstalling.\n[kind: answer]\n[confidence: 0.3]"),
                _response("Delete config/dragonminez and restart.\n[kind: answer]\n[confidence: 0.9]", response_id="r2"),
            ]
        )

        self.assertEqual(create_response.await_count, 2)
        second = create_response.await_args_list[1].kwargs
        self.assertEqual(second["model"], "gpt-5")
        self.assertEqual(second["reasoning"]["effort"], "medium")
        tool_names = [tool.get("name") or tool.get("type") for tool in second["tools"]]
        self.assertIn("file_search", tool_names)
        self.assertIn("get_patreon_status", tool_names)
        self.assertEqual(result["reply"], "Delete config/dragonminez and restart.")
        self.assertTrue(result["escalated"])
        self.assertEqual(
            [call.args[0].workflow for call in record_trace.await_args_list],
            ["support_draft", "support_escalation"],
        )
        self.assertEqual(ai_budget.used(ai_budget.BILLED), 100)

    async def test_confident_clarify_and_missing_markers_do_not_escalate(self) -> None:
        for text in ("Which Minecraft version?\n[kind: clarify]\n[confidence: 0.4]", "Plain answer without markers."):
            _, create_response, _ = await self._run([_response(text)])
            create_response.assert_awaited_once()

    async def test_escalation_falls_back_to_free_mini_high_effort_when_budgets_are_spent(self) -> None:
        result, create_response, _ = await self._run(
            [
                _response("Not sure.\n[kind: handoff]\n[confidence: 0.2]"),
                _response("Try /dmzrestoreupdate.\n[kind: answer]\n[confidence: 0.8]"),
            ],
            openai_daily_billed_token_limit=0,
            openai_daily_big_token_limit=0,
        )

        second = create_response.await_args_list[1].kwargs
        self.assertEqual((second["model"], second["reasoning"]["effort"]), ("gpt-5-mini", "high"))
        self.assertNotIn("tools", second)
        self.assertTrue(result["escalated"])

    async def test_paused_when_small_pool_is_spent(self) -> None:
        result, create_response, _ = await self._run([], openai_daily_small_token_limit=0)

        create_response.assert_not_awaited()
        self.assertTrue(result["paused"])

    async def test_tool_loop_chains_call_outputs_and_survives_tool_errors(self) -> None:
        async def broken_tool(**_kwargs):
            raise RuntimeError("db down")

        releases = AsyncMock(return_value={"latest_public_release": {"file": "dmz-2.1.1.jar"}})
        with patch.dict(
            tools_registry.TOOLS_FUNCS,
            {"get_latest_releases": releases, "get_user_bug_reports": broken_tool},
        ):
            result, create_response, record_trace = await self._run(
                [
                    _response("Unsure.\n[kind: answer]\n[confidence: 0.1]"),
                    _response(
                        response_id="r_tools",
                        output=[
                            _function_call("get_latest_releases", "call_1"),
                            _function_call("get_user_bug_reports", "call_2", '{"discord_user_id": "555"}'),
                        ],
                    ),
                    _response("Latest is 2.1.1.\n[kind: answer]\n[confidence: 0.9]", response_id="r_final"),
                ]
            )

        followup = create_response.await_args_list[2].kwargs
        self.assertEqual(followup["previous_response_id"], "r_tools")
        self.assertIn("tools", followup)
        outputs = {item["call_id"]: item for item in followup["input"]}
        self.assertEqual(json.loads(outputs["call_1"]["output"])["latest_public_release"]["file"], "dmz-2.1.1.jar")
        self.assertEqual(json.loads(outputs["call_2"]["output"]), {"error": "tool_failed"})
        self.assertEqual(result["tool_results"][1]["arguments"], {"discord_user_id": "123"})
        self.assertEqual(result["reply"], "Latest is 2.1.1.")
        trace = record_trace.await_args.args[0]
        self.assertEqual(trace.previous_response_id, "r_tools")
        self.assertEqual(trace.total_tokens, 200)
        self.assertIn("get_latest_releases", trace.tool_names)

    async def test_repeated_reply_escalates_with_note_and_hands_off_if_still_repeating(self) -> None:
        earlier = "Update GeckoLib to 4.8.3 and TerraBlender to 3.0.1.10, then restart the game."
        messages = [
            {"role": "user", "content": "It crashes", "speaker_id": "123", "speaker_kind": "requester"},
            {"role": "assistant", "content": earlier},
            {"role": "user", "content": "still crashes", "speaker_id": "123", "speaker_kind": "requester"},
        ]
        result, create_response, _ = await self._run(
            [
                _response(earlier + "\n[kind: answer]\n[confidence: 0.9]"),
                _response(earlier + "\n[kind: answer]\n[confidence: 0.9]"),
            ],
            messages=messages,
        )

        second_input = create_response.await_args_list[1].kwargs["input"]
        self.assertEqual(second_input[-1], {"role": "developer", "content": REPEAT_NOTE})
        self.assertEqual(result["kind"], "handoff")


if __name__ == "__main__":
    unittest.main()
