import os
import unittest


os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.cogs.ai_tickets import _chunk_discord_message


class DiscordMessageChunkTests(unittest.TestCase):
    def test_chunks_long_messages_under_discord_limit(self) -> None:
        text = ("alpha beta gamma\n" * 180).strip()

        chunks = _chunk_discord_message(text, limit=500)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(0 < len(chunk) <= 500 for chunk in chunks))
        self.assertEqual("".join(chunks), text)

    def test_preserves_short_messages(self) -> None:
        self.assertEqual(_chunk_discord_message("Short answer."), ["Short answer."])


if __name__ == "__main__":
    unittest.main()
