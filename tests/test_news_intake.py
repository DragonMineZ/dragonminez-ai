import os
import unittest

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bulmaai.services import ai_budget, news_intake as news
from v2_helpers import buttons, text

SETTINGS = SimpleNamespace(news_model="claude-haiku-5-5", news_max_searches=5, ai_daily_small_token_limit=10**9)
TASK = news.NewsTask(
    id=1,
    name="claude-models",
    prompt="New Claude model releases or announcements from Anthropic",
    interval_hours=6,
    enabled=True,
    next_run_at=datetime(2026, 11, 9, tzinfo=timezone.utc),
    last_run_at=None,
    last_error=None,
    created_by=None,
)


def search_block(*urls):
    return SimpleNamespace(
        type="web_search_tool_result",
        content=[SimpleNamespace(type="web_search_result", url=url, title="t") for url in urls],
    )


def text_block(value):
    return SimpleNamespace(type="text", text=value)


def response(*content, stop_reason="end_turn"):
    return SimpleNamespace(content=list(content), stop_reason=stop_reason, model="claude-haiku-5-5", usage=None)


class NormalizeUrlTests(unittest.TestCase):
    def test_lowercases_scheme_and_host_and_drops_fragment(self):
        self.assertEqual(news.normalize_url("HTTPS://WWW.Anthropic.com/News/Claude#top"), "https://www.anthropic.com/News/Claude")

    def test_drops_tracking_params_and_keeps_the_rest(self):
        self.assertEqual(
            news.normalize_url("https://a.com/p/?utm_source=x&id=3&ref=hn&fbclid=1&gclid=2&UTM_Medium=y"),
            "https://a.com/p?id=3",
        )

    def test_strips_trailing_slash(self):
        self.assertEqual(news.normalize_url("https://a.com/post/"), "https://a.com/post")
        self.assertEqual(news.normalize_url("https://a.com/"), "https://a.com")

    def test_refuses_non_http(self):
        for url in ("javascript:alert(1)", "ftp://a.com/x", "mailto:a@b.c", "", "https://", "https://a.com/x y", "not a url"):
            self.assertEqual(news.normalize_url(url), "", url)


class ParseItemsTests(unittest.TestCase):
    def test_fenced_json(self):
        self.assertEqual(news.parse_items('```json\n{"items": [{"title": "A"}]}\n```'), [{"title": "A"}])

    def test_prose_around_json(self):
        self.assertEqual(news.parse_items('Here you go: {"items": [{"title": "A"}]} Hope it helps.'), [{"title": "A"}])

    def test_garbage_is_empty(self):
        for value in ("", "nothing new", "{not json}", '{"items": "x"}', "[1, 2]", None):
            self.assertEqual(news.parse_items(value), [], value)


class FilterTests(unittest.TestCase):
    def test_drops_items_whose_url_wasnt_in_the_results(self):
        allowed = news.result_urls([search_block("https://anthropic.com/news/claude-6?utm_source=x")])
        items = news.clean_items(
            [
                {"title": "Real", "url": "https://Anthropic.com/news/claude-6/", "summary": "s"},
                {"title": "Made up", "url": "https://anthropic.com/news/claude-7"},
                {"title": "Bad scheme", "url": "javascript:alert(1)"},
                {"title": "", "url": "https://anthropic.com/news/claude-6"},
            ],
            allowed,
        )
        self.assertEqual([item["title"] for item in items], ["Real"])
        self.assertEqual(items[0]["url"], "https://anthropic.com/news/claude-6")

    def test_error_result_has_no_urls(self):
        error = SimpleNamespace(type="web_search_tool_result", content=SimpleNamespace(type="web_search_tool_result_error", error_code="unavailable"))
        self.assertEqual(news.result_urls([error, text_block("hi")]), set())


class CardTests(unittest.IsolatedAsyncioTestCase):
    async def test_card_defuses_mentions(self):
        view = news.item_card(
            "claude-models",
            {"title": "@everyone Claude 6 is out <@123>", "summary": "Hey @here, <@&456> look", "url": "https://a.com/x", "published": "2026-11-08"},
        )
        body = text(view)
        self.assertIn("## 📰", body)
        self.assertIn("[Source](<https://a.com/x>)", body)
        self.assertIn("2026-11-08", body)
        self.assertNotIn("@everyone", body)
        self.assertNotIn("@here", body)
        self.assertNotIn("<@123>", body)
        self.assertNotIn("<@&456>", body)
        self.assertEqual(buttons(view), [])

    async def test_card_escapes_model_written_links(self):
        view = news.item_card("t", {"title": "News", "summary": "see [here](https://evil.example)", "url": "https://a.com/x"})
        self.assertNotIn("[here](https://evil.example)", text(view))

    async def test_tasks_card_lists_tasks(self):
        body = text(news.tasks_card([TASK]))
        self.assertIn("claude-models", body)
        self.assertIn("every 6h", body)


class SearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_paused_budget_returns_nothing(self):
        client = MagicMock()
        client.messages.create = AsyncMock()
        with patch.object(ai_budget, "is_paused", return_value=True):
            self.assertEqual(await news.search(TASK, [], SETTINGS, client=client), [])
        client.messages.create.assert_not_called()

    async def test_search_keeps_only_returned_urls_and_continues_after_pause(self):
        client = MagicMock()
        client.messages.create = AsyncMock(
            side_effect=[
                response(search_block("https://anthropic.com/news/claude-6"), stop_reason="pause_turn"),
                response(
                    text_block(
                        '{"items": [{"title": "Claude 6", "url": "https://anthropic.com/news/claude-6", "summary": "Out."},'
                        ' {"title": "Fake", "url": "https://evil.example/x"}]}'
                    )
                ),
            ]
        )
        with patch.object(ai_budget, "is_paused", return_value=False), patch.object(ai_budget, "record_response"):
            items = await news.search(TASK, [{"title": "Old", "url": "https://a.com/old"}], SETTINGS, client=client)
        self.assertEqual([item["title"] for item in items], ["Claude 6"])
        self.assertEqual(client.messages.create.await_count, 2)
        kwargs = client.messages.create.await_args.kwargs
        self.assertEqual(kwargs["tools"][0]["type"], "web_search_20250305")
        self.assertEqual(kwargs["messages"][1]["role"], "assistant")
        self.assertIn("https://a.com/old", kwargs["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
