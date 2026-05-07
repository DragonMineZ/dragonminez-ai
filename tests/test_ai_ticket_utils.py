import os
import types
import unittest
import asyncio
from unittest.mock import AsyncMock, patch


os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.cogs.ai_tickets import (
    AITicketsCog,
    _chunk_discord_message,
    _has_user_visible_tool_result,
    _is_staff_ticket_message,
    _support_debounce_seconds,
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

    def test_detects_user_visible_tool_result(self) -> None:
        self.assertTrue(
            _has_user_visible_tool_result(
                [
                    {"name": "docs_search", "output": {"matches": []}},
                    {
                        "name": "start_patreon_whitelist_flow",
                        "output": {"status": "ok", "suppress_ai_reply": True},
                    },
                ]
            )
        )

    def test_ignores_non_visible_tool_result(self) -> None:
        self.assertFalse(
            _has_user_visible_tool_result(
                [{"name": "docs_search", "output": {"matches": []}}]
            )
        )

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
            types.SimpleNamespace(filename="one.png", content_type="image/png", url="https://cdn.example/one.png"),
            types.SimpleNamespace(filename="two.png", content_type="image/png", url="https://cdn.example/two.png"),
        ]
        message = types.SimpleNamespace(attachments=attachments)

        async def fake_create(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                first_started.set()
                await asyncio.wait_for(second_started.wait(), timeout=0.5)
            else:
                second_started.set()
                await asyncio.wait_for(first_started.wait(), timeout=0.5)
            return types.SimpleNamespace(output_text="image details")

        with patch(
            "bulmaai.cogs.ai_tickets.vision_client.responses.create",
            new_callable=AsyncMock,
            side_effect=fake_create,
        ):
            result = await asyncio.wait_for(cog._extract_image_context(message), timeout=1.0)

        self.assertEqual(result, "image details\nimage details")


if __name__ == "__main__":
    unittest.main()
