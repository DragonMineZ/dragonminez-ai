import os
import unittest

import discord

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.services.showcase import should_highlight_message
from bulmaai.ui.showcase_views import build_showcase_highlight_card
from v2_helpers import buttons, text, walk


class ShowcaseDecisionTests(unittest.TestCase):
    def _decide(self, **overrides) -> bool:
        params = dict(
            reaction_count=5,
            threshold=5,
            channel_id=100,
            source_channel_ids=(100,),
            is_bot_author=False,
        )
        params.update(overrides)
        return should_highlight_message(**params)

    def test_highlights_when_threshold_met_in_source_channel(self) -> None:
        self.assertTrue(self._decide())

    def test_highlights_when_reaction_count_exceeds_threshold(self) -> None:
        self.assertTrue(self._decide(reaction_count=9))

    def test_ignores_message_below_threshold(self) -> None:
        self.assertFalse(self._decide(reaction_count=4))

    def test_ignores_message_outside_source_channels(self) -> None:
        self.assertFalse(self._decide(channel_id=999))

    def test_ignores_bot_authored_messages(self) -> None:
        self.assertFalse(self._decide(is_bot_author=True))

    def test_ignores_when_no_source_channels_configured(self) -> None:
        self.assertFalse(self._decide(source_channel_ids=()))


class ShowcaseCardTests(unittest.IsolatedAsyncioTestCase):  # py-cord views need a running loop
    def _card(self, **overrides):
        kwargs = dict(
            author_name="Trunks#0001",
            author_avatar_url="https://cdn.example/avatar.png",
            content="Check out my new build!",
            image_url="https://cdn.example/screenshot.png",
            reaction_count=7,
            reaction_emoji="⭐",
            jump_url="https://discord.com/channels/1/2/3",
        )
        return build_showcase_highlight_card(**(kwargs | overrides))

    async def test_card_carries_author_content_reactions_and_jump_link(self) -> None:
        view = self._card()
        card_text = text(view)

        self.assertIn("-# by **Trunks#0001**", card_text)
        self.assertIn("Check out my new build!", card_text)
        self.assertIn("-# ⭐ 7 reactions", card_text)
        kinds = {type(item) for item in walk(view)}
        self.assertIn(discord.ui.Thumbnail, kinds)
        self.assertIn(discord.ui.MediaGallery, kinds)
        [button] = buttons(view)
        self.assertEqual(button.url, "https://discord.com/channels/1/2/3")

    async def test_card_omits_image_and_avatar_when_none_provided(self) -> None:
        kinds = {type(item) for item in walk(self._card(image_url=None, author_avatar_url=None))}
        self.assertNotIn(discord.ui.MediaGallery, kinds)
        self.assertNotIn(discord.ui.Thumbnail, kinds)

    async def test_card_truncates_long_content(self) -> None:
        card_text = text(self._card(content="x" * 5000))
        self.assertLessEqual(len(card_text), 4000)
        self.assertIn("x…", card_text)


if __name__ == "__main__":
    unittest.main()
