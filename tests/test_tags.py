import asyncio
import os
import unittest

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from unittest.mock import AsyncMock, patch

from bulmaai.cogs.tags import list_text, tag_card
from bulmaai.services import tags
from tests.v2_helpers import texts


class TagTests(unittest.TestCase):
    def test_clean_name(self):
        self.assertEqual(tags.clean_name("  Mixin Apply "), "mixin-apply")
        for bad in ("", "-x", "a" * 41, "no/slash", "<@1>"):
            with self.assertRaises(ValueError):
                tags.clean_name(bad)

    def test_clean_body(self):
        with self.assertRaises(ValueError):
            tags.clean_body("   ")
        with self.assertRaises(ValueError):
            tags.clean_body("x" * (tags.BODY_MAX + 1))

    def test_save_rejects_unknown_category(self):
        with self.assertRaises(ValueError):
            asyncio.run(tags.save("nope", "a", "b", 1))

    def test_save_upserts(self):
        pool = AsyncMock()
        with patch.object(tags, "get_pool", AsyncMock(return_value=pool)):
            tag = asyncio.run(tags.save("api", "Ki Blast", " text ", 7))
        self.assertEqual(tag, tags.Tag("api", "ki-blast", "text"))
        self.assertIn("ON CONFLICT", pool.execute.call_args.args[0])


class TagCardTests(unittest.IsolatedAsyncioTestCase):
    async def test_card_has_no_pings(self):
        shown = " ".join(texts(tag_card(tags.Tag("error", "x", "hi @everyone <@123>"))))
        self.assertIn("error/x", shown)
        self.assertNotIn("<@123>", shown)
        self.assertNotIn("@everyone", shown)


class TagListTests(unittest.TestCase):
    def test_list_groups_by_category(self):
        shown = list_text([("api", "a"), ("api", "b"), ("mixin", "c")])
        self.assertIn("**api**: `a`, `b`", shown)
        self.assertIn("**mixin**: `c`", shown)
        self.assertIn("No tags", list_text([]))


if __name__ == "__main__":
    unittest.main()
