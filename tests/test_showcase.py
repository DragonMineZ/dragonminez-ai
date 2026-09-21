import os
import unittest

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.services.showcase import should_highlight_message
from bulmaai.ui.showcase_views import build_showcase_highlight_embed


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


class ShowcaseEmbedTests(unittest.TestCase):
    def test_embed_carries_author_content_reactions_and_jump_link(self) -> None:
        embed = build_showcase_highlight_embed(
            author_name="Trunks#0001",
            author_avatar_url="https://cdn.example/avatar.png",
            content="Check out my new build!",
            image_url="https://cdn.example/screenshot.png",
            reaction_count=7,
            reaction_emoji="⭐",
            jump_url="https://discord.com/channels/1/2/3",
        )

        self.assertEqual(embed.author.name, "Trunks#0001")
        self.assertEqual(embed.author.icon_url, "https://cdn.example/avatar.png")
        self.assertEqual(embed.description, "Check out my new build!")
        self.assertEqual(embed.image.url, "https://cdn.example/screenshot.png")

        field_values = {field.name: field.value for field in embed.fields}
        self.assertEqual(field_values["Reactions"], "⭐ 7")
        self.assertIn("https://discord.com/channels/1/2/3", field_values["Original"])

    def test_embed_omits_image_when_none_provided(self) -> None:
        embed = build_showcase_highlight_embed(
            author_name="Goten#0002",
            author_avatar_url=None,
            content="No screenshot here",
            image_url=None,
            reaction_count=5,
            reaction_emoji="⭐",
            jump_url="https://discord.com/channels/1/2/4",
        )

        self.assertFalse(embed.image)

    def test_embed_truncates_long_content(self) -> None:
        embed = build_showcase_highlight_embed(
            author_name="Vegeta#0003",
            author_avatar_url=None,
            content="x" * 5000,
            image_url=None,
            reaction_count=5,
            reaction_emoji="⭐",
            jump_url="https://discord.com/channels/1/2/5",
        )

        self.assertLessEqual(len(embed.description), 4096)
        self.assertTrue(embed.description.endswith("..."))


if __name__ == "__main__":
    unittest.main()
