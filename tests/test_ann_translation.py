import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.cogs.ai_ann_translation import AiAnnTranslation, build_announcement_sends, translate_text


class BuildAnnouncementSendsTests(unittest.TestCase):
    def test_short_text_is_one_message_carrying_the_files(self) -> None:
        files = ["image.png"]

        sends = build_announcement_sends("Nueva actualizacion!", files)

        self.assertEqual(sends, [("Nueva actualizacion!", files)])

    def test_long_text_is_split_under_the_limit(self) -> None:
        text = " ".join(["palabra"] * 800)

        sends = build_announcement_sends(text, [])

        self.assertGreater(len(sends), 1)
        for content, _ in sends:
            self.assertLessEqual(len(content), 2000)

    def test_split_preserves_every_word(self) -> None:
        text = " ".join(f"w{index}" for index in range(900))

        rejoined = "".join(content for content, _ in build_announcement_sends(text, []))

        self.assertEqual(rejoined.split(), text.split())

    def test_files_ride_the_final_message_only(self) -> None:
        files = ["a.png", "b.png"]

        sends = build_announcement_sends(" ".join(["palabra"] * 800), files)

        self.assertEqual(sends[-1][1], files)
        for _, chunk_files in sends[:-1]:
            self.assertEqual(chunk_files, [])

    def test_attachment_only_announcement_still_sends(self) -> None:
        files = ["clip.mp4"]

        self.assertEqual(build_announcement_sends("", files), [(None, files)])

    def test_empty_announcement_sends_nothing(self) -> None:
        self.assertEqual(build_announcement_sends("", []), [])


class AutoPublishTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompts_with_buttons_are_not_crossposted_but_link_posts_are(self) -> None:
        cog = AiAnnTranslation.__new__(AiAnnTranslation)
        cog.bot = SimpleNamespace(
            settings=SimpleNamespace(
                announcement_source_channel_id=None,
                announcement_spanish_channel_id=None,
                announcement_portuguese_channel_id=None,
                releases_channel_id=5,
                sneak_peeks_channel_id=None,
                patreon_announcement_channel_id=None,
            )
        )

        def message(*children):
            return SimpleNamespace(
                author="bot",
                channel=SimpleNamespace(id=5, type=discord.ChannelType.news),
                components=[SimpleNamespace(children=list(children))],
                publish=AsyncMock(),
            )

        prompt = message(SimpleNamespace(custom_id="approve", url=None))
        link_post = message(SimpleNamespace(custom_id=None, url="https://www.patreon.com/posts/1"))
        await cog.on_message_publish(prompt)
        await cog.on_message_publish(link_post)
        prompt.publish.assert_not_awaited()
        link_post.publish.assert_awaited_once()


class TranslateTextTests(unittest.IsolatedAsyncioTestCase):
    async def test_translate_text_uses_the_messages_api(self) -> None:
        response = SimpleNamespace(content=[SimpleNamespace(type="text", text="  Hola  ")], stop_reason="end_turn")
        cog = SimpleNamespace(
            client=SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=response))),
            settings=SimpleNamespace(ai_translation_model="claude-haiku-5-5"),
        )

        self.assertEqual(await translate_text(cog, "Hello", "es"), "Hola")

        kwargs = cog.client.messages.create.await_args.kwargs
        self.assertEqual(kwargs["model"], "claude-haiku-5-5")
        self.assertIn("Spanish", kwargs["system"])
        self.assertEqual(kwargs["messages"], [{"role": "user", "content": "Hello"}])
        self.assertGreaterEqual(kwargs["max_tokens"], 4000)
        self.assertEqual(kwargs["output_config"], {"effort": "low"})

    async def test_refusal_gives_empty_translation(self) -> None:
        response = SimpleNamespace(content=[], stop_reason="refusal")
        cog = SimpleNamespace(
            client=SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=response))),
            settings=SimpleNamespace(ai_translation_model="claude-haiku-5-5"),
        )

        self.assertEqual(await translate_text(cog, "Hello", "pt"), "")


if __name__ == "__main__":
    unittest.main()
