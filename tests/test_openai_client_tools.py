import os
import types
import unittest
from unittest.mock import AsyncMock, patch


os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.services.openai_client import (
    _handle_tools_and_final_reply,
    _looks_like_patreon_whitelist_request,
    _select_reasoning_effort,
    run_support_agent,
)


class OpenAIClientToolTests(unittest.IsolatedAsyncioTestCase):
    def test_detects_clear_patreon_whitelist_request(self) -> None:
        self.assertTrue(
            _looks_like_patreon_whitelist_request(
                [{"role": "user", "content": "Can I get Patreon whitelist access? My IGN is Test_User"}]
            )
        )
        self.assertFalse(
            _looks_like_patreon_whitelist_request(
                [{"role": "user", "content": "How do I become Super Saiyan?"}]
            )
        )

    def test_selects_fast_reasoning_for_high_confidence_docs(self) -> None:
        settings = types.SimpleNamespace(
            openai_support_reasoning_effort="medium",
            openai_support_fast_reasoning_effort="low",
            openai_support_fast_confidence_score=0.78,
        )

        self.assertEqual(
            _select_reasoning_effort(settings, {"best_score": 0.8}),
            "low",
        )

    def test_selects_default_reasoning_for_low_confidence_docs(self) -> None:
        settings = types.SimpleNamespace(
            openai_support_reasoning_effort="medium",
            openai_support_fast_reasoning_effort="low",
            openai_support_fast_confidence_score=0.78,
        )

        self.assertEqual(
            _select_reasoning_effort(settings, {"best_score": 0.4}),
            "medium",
        )

    async def test_suppress_ai_reply_tool_skips_followup_model_call(self) -> None:
        function_call = types.SimpleNamespace(
            type="function_call",
            name="start_patreon_whitelist_flow",
            arguments='{"nickname": "TestUser"}',
        )
        response = types.SimpleNamespace(output=[function_call], output_text="")

        async def fake_tool(**kwargs):
            return {
                "status": "ok",
                "message": "flow_started",
                "suppress_ai_reply": True,
                "user_message_sent": True,
            }

        with (
            patch("bulmaai.services.openai_client.tools_registry.get_func", return_value=fake_tool),
            patch("bulmaai.services.openai_client._create_response", new_callable=AsyncMock) as create_response,
        ):
            result = await _handle_tools_and_final_reply(
                response=response,
                base_input=[],
                base_tool_results=[],
                system_prompt="system",
                model="gpt-5-mini",
                lang="en",
                settings=types.SimpleNamespace(
                    openai_support_max_output_tokens=100,
                    ai_support_timeout_seconds=1,
                    openai_support_reasoning_effort="medium",
                ),
                user_id=123,
                channel_id=456,
                bot=object(),
            )

        self.assertEqual(result["reply"], "(no reply)")
        self.assertFalse(result["suggested_close"])
        self.assertEqual(result["tool_results"][0]["name"], "start_patreon_whitelist_flow")
        create_response.assert_not_awaited()

    async def test_clear_patreon_whitelist_request_skips_docs_and_model(self) -> None:
        async def fake_whitelist_tool(**kwargs):
            return {
                "status": "ok",
                "message": "asked_for_nickname",
                "suppress_ai_reply": True,
            }

        def fake_get_func(name, bot_context=None):
            if name == "start_patreon_whitelist_flow":
                return fake_whitelist_tool
            raise AssertionError(f"unexpected tool call: {name}")

        with (
            patch("bulmaai.services.openai_client.tools_registry.get_func", side_effect=fake_get_func),
            patch(
                "bulmaai.services.openai_client._create_response",
                new_callable=AsyncMock,
                return_value=types.SimpleNamespace(output=[], output_text="model fallback"),
            ) as create_response,
        ):
            result = await run_support_agent(
                messages=[
                    {
                        "role": "user",
                        "content": "Please start Patreon whitelist access. My IGN is Test_User",
                        "speaker_id": "123",
                    }
                ],
                enabled_tools=["docs_search", "start_patreon_whitelist_flow"],
                user_id=123,
                channel_id=456,
                bot=object(),
                settings=types.SimpleNamespace(
                    openai_support_model="gpt-5-mini",
                    openai_model="gpt-5-mini",
                    openai_support_max_output_tokens=100,
                    ai_support_timeout_seconds=1,
                    openai_support_reasoning_effort="medium",
                    support_response_cache_enabled=False,
                ),
            )

        self.assertEqual(result["reply"], "(no reply)")
        self.assertEqual(result["tool_results"][0]["name"], "start_patreon_whitelist_flow")
        self.assertEqual(result["tool_results"][0]["arguments"]["nickname"], "Test_User")
        create_response.assert_not_awaited()

    async def test_high_confidence_approved_faq_skips_model_call(self) -> None:
        async def fake_docs_search(**kwargs):
            return {
                "matches": [
                    {
                        "source_type": "approved_faq",
                        "faq_id": 7,
                        "title": "How do I transform?",
                        "answer": "Use the configured form key.",
                        "content": "Use the configured form key.",
                        "lang": "en",
                        "score": 0.92,
                    }
                ],
                "suggested_answers": [],
                "best_score": 0.92,
                "best_source_type": "approved_faq",
                "confidence": "high",
            }

        def fake_get_func(name, bot_context=None):
            if name == "docs_search":
                return fake_docs_search
            raise AssertionError(f"unexpected tool call: {name}")

        with (
            patch("bulmaai.services.openai_client.tools_registry.get_func", side_effect=fake_get_func),
            patch("bulmaai.services.openai_client._create_response", new_callable=AsyncMock) as create_response,
        ):
            result = await run_support_agent(
                messages=[
                    {
                        "role": "user",
                        "content": "How do I transform?",
                        "speaker_id": "123",
                    }
                ],
                enabled_tools=["docs_search"],
                language_hint="en",
                use_cache=False,
                user_id=123,
                channel_id=456,
                settings=types.SimpleNamespace(
                    openai_support_model="gpt-5-mini",
                    openai_model="gpt-5-mini",
                    openai_support_max_output_tokens=100,
                    ai_support_timeout_seconds=1,
                    openai_support_reasoning_effort="medium",
                    support_response_cache_enabled=False,
                ),
            )

        self.assertEqual(result["reply"], "Use the configured form key.")
        self.assertEqual(result["tool_results"][0]["name"], "docs_search")
        self.assertFalse(result["suggested_close"])
        create_response.assert_not_awaited()

    async def test_high_confidence_fallback_language_faq_still_uses_model(self) -> None:
        async def fake_docs_search(**kwargs):
            return {
                "matches": [
                    {
                        "source_type": "approved_faq",
                        "faq_id": 7,
                        "title": "How do I transform?",
                        "answer": "Use the configured form key.",
                        "content": "Use the configured form key.",
                        "lang": "en",
                        "score": 0.92,
                    }
                ],
                "suggested_answers": [],
                "best_score": 0.92,
                "best_source_type": "approved_faq",
                "confidence": "high",
            }

        def fake_get_func(name, bot_context=None):
            if name == "docs_search":
                return fake_docs_search
            raise AssertionError(f"unexpected tool call: {name}")

        with (
            patch("bulmaai.services.openai_client.tools_registry.get_func", side_effect=fake_get_func),
            patch(
                "bulmaai.services.openai_client._create_response",
                new_callable=AsyncMock,
                return_value=types.SimpleNamespace(output=[], output_text="Usa la tecla configurada."),
            ) as create_response,
        ):
            result = await run_support_agent(
                messages=[
                    {
                        "role": "user",
                        "content": "Como me transformo?",
                        "speaker_id": "123",
                    }
                ],
                enabled_tools=["docs_search"],
                language_hint="es",
                use_cache=False,
                user_id=123,
                channel_id=456,
                settings=types.SimpleNamespace(
                    openai_support_model="gpt-5-mini",
                    openai_model="gpt-5-mini",
                    openai_support_max_output_tokens=100,
                    ai_support_timeout_seconds=1,
                    openai_support_reasoning_effort="medium",
                    openai_support_fast_reasoning_effort="low",
                    openai_support_fast_confidence_score=0.78,
                    support_response_cache_enabled=False,
                ),
            )

        self.assertEqual(result["reply"], "Usa la tecla configurada.")
        create_response.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
