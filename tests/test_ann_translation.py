import os
import unittest

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.cogs.ai_ann_translation import build_announcement_sends


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


if __name__ == "__main__":
    unittest.main()
