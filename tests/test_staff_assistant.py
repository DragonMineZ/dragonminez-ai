import json
import os
import sys
import types
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

try:
    from bulmaai.services import staff_tools  # noqa: F401
except ImportError:
    stub = types.ModuleType("bulmaai.services.staff_tools")
    stub.ToolContext = lambda **kwargs: SimpleNamespace(**kwargs)
    stub.tools_for = lambda tier: []
    stub.run_tool = AsyncMock(return_value={})
    sys.modules["bulmaai.services.staff_tools"] = stub
    import bulmaai.services

    bulmaai.services.staff_tools = stub

from bulmaai.services import ai_budget, staff_assistant
from bulmaai.web.core import Tier

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"


def text_response(text, model=HAIKU, stop_reason="end_turn"):
    content = [SimpleNamespace(type="text", text=text)] if text else []
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=None, model=model, id="r")


def tool_response(name, args, call_id="t1", model=HAIKU):
    block = SimpleNamespace(type="tool_use", id=call_id, name=name, input=args)
    return SimpleNamespace(content=[block], stop_reason="tool_use", usage=None, model=model, id="r")


def settings(**overrides):
    values = dict(
        staff_ai_enabled=True,
        staff_ai_model=HAIKU,
        staff_ai_escalation_model=SONNET,
        staff_ai_daily_requests=60,
        ai_support_timeout_seconds=30,
        panel_guild_id=1,
        moderation_banned_words=(),
        moderation_blocked_domains=(),
        wiki_base_url="https://wiki.dragonminez.com",
        announcement_role_en_id=10,
        announcement_role_es_id=20,
        announcement_role_pt_id=30,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def member(*role_ids):
    return SimpleNamespace(
        id=5, display_name="Tester", guild=SimpleNamespace(id=1), roles=[SimpleNamespace(id=r) for r in role_ids]
    )


PROFILE = {"user_id": 5, "nickname": "Nick", "language": None, "timezone": "UTC", "ticket_info": True}


class AskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ai_budget.reset()
        self.settings = settings()
        self.bot = SimpleNamespace(settings=self.settings, get_guild=lambda _id: SimpleNamespace(id=1))
        self.run_tool = AsyncMock(return_value={"ok": True})
        patches = [
            patch.object(staff_assistant.staff_store, "get_profile", AsyncMock(return_value=dict(PROFILE))),
            patch.object(staff_assistant.staff_store, "list_notes", AsyncMock(return_value=[])),
            patch.object(staff_assistant.staff_store, "count_requests_since", AsyncMock(return_value=0)),
            patch.object(staff_assistant.staff_store, "log_request", AsyncMock()),
            patch.object(staff_assistant, "tier_for", lambda m, s: Tier.MODERATOR),
            patch.object(staff_assistant.staff_tools, "tools_for", lambda tier: [{"name": "member_lookup"}]),
            patch.object(staff_assistant.staff_tools, "run_tool", self.run_tool),
            patch.object(staff_assistant.staff_tools, "ToolContext", lambda **kw: SimpleNamespace(**kw), create=True),
        ]
        for each in patches:
            each.start()
            self.addCleanup(each.stop)

    async def ask(self, responses):
        with patch("bulmaai.services.ai_client._create_message", AsyncMock(side_effect=responses)) as create:
            self.create = create
            return await staff_assistant.ask(
                bot=self.bot, member=member(), text="hi", history=[], origin="discord", channel_id=None
            )

    def models(self):
        return [call.kwargs["model"] for call in self.create.await_args_list]

    async def test_haiku_answer_does_not_escalate(self):
        reply = await self.ask([text_response("All good.")])
        self.assertEqual(reply.text, "All good.")
        self.assertEqual(self.models(), [HAIKU])
        self.assertEqual(self.create.await_args.kwargs["output_config"], {"effort": "medium"})

    async def test_refusal_escalates_once_to_sonnet(self):
        reply = await self.ask([text_response("", stop_reason="refusal"), text_response("From Sonnet.", SONNET)])
        self.assertEqual(reply.text, "From Sonnet.")
        self.assertEqual(self.models(), [HAIKU, SONNET])
        self.assertEqual(self.create.await_args.kwargs["output_config"], {"effort": "low"})

    async def test_empty_text_escalates(self):
        reply = await self.ask([text_response(""), text_response("Second.", SONNET)])
        self.assertEqual(reply.text, "Second.")
        self.assertEqual(self.models(), [HAIKU, SONNET])

    async def test_out_of_tool_rounds_escalates(self):
        looping = [tool_response("member_lookup", {"user": "x"}, f"t{i}") for i in range(7)]
        reply = await self.ask([*looping, text_response("Done.", SONNET)])
        self.assertEqual(self.models(), [HAIKU] * 7 + [SONNET])
        self.assertEqual(reply.text, "Done.")
        self.assertEqual(self.run_tool.await_count, 6)

    async def test_two_failures_give_fallback_and_one_sonnet_pass(self):
        reply = await self.ask([text_response("", stop_reason="max_tokens"), text_response("")])
        self.assertEqual(self.models(), [HAIKU, SONNET])
        self.assertEqual(reply.text, staff_assistant.TEXT["en"]["fallback"])

    async def test_tool_loop_collects_pending_actions(self):
        self.run_tool.side_effect = [{"pending_action": 7, "summary": "Warn x"}, {"pending_action": 9, "summary": "y"}]
        reply = await self.ask(
            [
                tool_response("warn", {"user": "1"}, "a"),
                tool_response("note", {"user": "1"}, "b"),
                text_response("Confirm with the button."),
            ]
        )
        self.assertEqual(reply.pending_action_ids, [7, 9])
        self.assertEqual(self.models(), [HAIKU] * 3)
        names = [call.args[0] for call in self.run_tool.await_args_list]
        self.assertEqual(names, ["warn", "note"])
        self.assertEqual(self.run_tool.await_args_list[0].args[2].tier, Tier.MODERATOR)

    async def test_tool_exception_becomes_error_result(self):
        self.run_tool.side_effect = RuntimeError("boom")
        reply = await self.ask([tool_response("member_lookup", {}), text_response("Sorry.")])
        self.assertEqual(reply.text, "Sorry.")
        followup = self.create.await_args_list[1].kwargs["messages"][-1]["content"][0]
        self.assertIn("tool_failed", followup["content"])

    async def test_paused_budget(self):
        with patch.object(staff_assistant.ai_budget, "is_paused", lambda s: True):
            reply = await self.ask([])
        self.assertEqual(reply.text, "AI is paused for today.")
        self.create.assert_not_awaited()

    async def test_daily_cap_refuses_without_calling(self):
        staff_assistant.staff_store.count_requests_since.return_value = 60
        reply = await self.ask([])
        self.assertIn("60", reply.text)
        self.create.assert_not_awaited()
        staff_assistant.staff_store.log_request.assert_not_awaited()

    async def test_request_is_logged(self):
        await self.ask([text_response("ok")])
        staff_assistant.staff_store.log_request.assert_awaited_once_with(5)

    async def test_guard_defuses_mentions(self):
        reply = await self.ask([text_response("Ask <@123456789012345678> about it.")])
        self.assertNotIn("<@123456789012345678>", reply.text)

    async def test_blocked_reply_becomes_fallback(self):
        reply = await self.ask([text_response("<function_calls> run it"), text_response("", SONNET)])
        self.assertEqual(reply.text, staff_assistant.TEXT["en"]["fallback"])

    async def test_non_staff_is_refused(self):
        with patch.object(staff_assistant, "tier_for", lambda m, s: Tier.NONE):
            reply = await self.ask([])
        self.assertEqual(reply.text, staff_assistant.TEXT["en"]["not_staff"])
        self.create.assert_not_awaited()

    async def test_static_prompt_cached_first(self):
        await self.ask([text_response("ok")])
        system = self.create.await_args.kwargs["system"]
        self.assertEqual(system[0]["cache_control"], {"type": "ephemeral"})
        self.assertIn("Nick", system[1]["text"])
        self.assertNotIn("cache_control", system[1])


class LanguageTests(unittest.TestCase):
    def test_profile_override_beats_roles(self):
        self.assertEqual(staff_assistant.language_for(member(20), {"language": "pt"}, settings()), "pt")

    def test_roles_used_without_profile_language(self):
        self.assertEqual(staff_assistant.language_for(member(20), {"language": None}, settings()), "es")
        self.assertEqual(staff_assistant.language_for(member(), None, settings()), "en")


class DistillTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ai_budget.reset()
        self.bot = SimpleNamespace(settings=settings())

    async def test_stores_at_most_25_notes_and_marks_distilled(self):
        response = text_response(json.dumps({"notes": [f"Note {i}" for i in range(40)]}))
        row = {"content": "hello", "channel_id": 1, "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}
        with (
            patch.object(
                staff_assistant.staff_store, "get_profile", AsyncMock(return_value=dict(PROFILE, distilled_at=None))
            ),
            patch.object(staff_assistant.staff_store, "recent_vault", AsyncMock(return_value=[row])),
            patch.object(staff_assistant.staff_store, "list_notes", AsyncMock(return_value=[])),
            patch.object(staff_assistant.mod_cases, "list_cases", AsyncMock(return_value=[])),
            patch.object(staff_assistant.staff_store, "replace_distilled", AsyncMock()) as replace,
            patch.object(staff_assistant.staff_store, "mark_distilled", AsyncMock()) as mark,
            patch("bulmaai.services.ai_client._create_message", AsyncMock(return_value=response)),
        ):
            await staff_assistant.distill(self.bot, 5)
        replace.assert_awaited_once()
        self.assertEqual(len(replace.await_args.args[1]), 25)
        mark.assert_awaited_once_with(5)

    async def test_nothing_new_makes_no_call(self):
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with (
            patch.object(
                staff_assistant.staff_store, "get_profile", AsyncMock(return_value=dict(PROFILE, distilled_at=since))
            ),
            patch.object(staff_assistant.staff_store, "vault_since", AsyncMock(return_value=[])),
            patch("bulmaai.services.ai_client._create_message", AsyncMock()) as create,
            patch.object(staff_assistant.staff_store, "mark_distilled", AsyncMock()) as mark,
        ):
            await staff_assistant.distill(self.bot, 5)
        create.assert_not_awaited()
        mark.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
