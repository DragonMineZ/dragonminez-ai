import asyncio
import os
import unittest
from unittest.mock import patch


os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.utils import docs_search


class DocsSearchLatencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetches_embedding_docs_and_faq_candidates_concurrently(self) -> None:
        embedding_started = asyncio.Event()
        docs_started = asyncio.Event()
        faqs_started = asyncio.Event()

        async def fake_embedding(query: str) -> list[float]:
            embedding_started.set()
            await asyncio.wait_for(docs_started.wait(), timeout=0.5)
            await asyncio.wait_for(faqs_started.wait(), timeout=0.5)
            return [1.0, 0.0]

        async def fake_docs(**kwargs):
            docs_started.set()
            await asyncio.wait_for(embedding_started.wait(), timeout=0.5)
            return []

        async def fake_faqs(**kwargs):
            faqs_started.set()
            await asyncio.wait_for(embedding_started.wait(), timeout=0.5)
            return []

        with (
            patch.object(docs_search, "_get_query_embedding", side_effect=fake_embedding),
            patch.object(docs_search, "_fetch_candidate_docs", side_effect=fake_docs),
            patch.object(docs_search, "_fetch_candidate_faqs", side_effect=fake_faqs),
        ):
            result = await asyncio.wait_for(
                docs_search.run_docs_search("how do I transform?", language="en"),
                timeout=1.0,
            )

        self.assertEqual(result["matches"], [])

    async def test_returns_approved_faq_metadata_in_search_results(self) -> None:
        async def fake_embedding(query: str) -> list[float]:
            return [1.0, 0.0]

        async def fake_docs(**kwargs):
            return []

        async def fake_faqs(**kwargs):
            return [
                {
                    "id": 7,
                    "faq_id": 7,
                    "source": "faq:7",
                    "source_type": "approved_faq",
                    "section": "FAQ",
                    "title": "How do I transform?",
                    "content": "Use the configured form key.",
                    "lang": "en",
                    "tags": ["forms", "controls"],
                    "embedding": [1.0, 0.0],
                    "lexical_rank": 1.0,
                }
            ]

        with (
            patch.object(docs_search, "_get_query_embedding", side_effect=fake_embedding),
            patch.object(docs_search, "_fetch_candidate_docs", side_effect=fake_docs),
            patch.object(docs_search, "_fetch_candidate_faqs", side_effect=fake_faqs),
        ):
            result = await docs_search.run_docs_search("how do I transform?", language="en")

        self.assertEqual(result["best_source_type"], "approved_faq")
        self.assertEqual(result["matches"][0]["source_type"], "approved_faq")
        self.assertEqual(result["matches"][0]["faq_id"], 7)
        self.assertEqual(result["matches"][0]["tags"], ["forms", "controls"])
        self.assertEqual(result["suggested_answers"][0]["source_type"], "approved_faq")

    async def test_faq_search_uses_language_fallbacks(self) -> None:
        seen_languages = None

        async def fake_embedding(query: str) -> list[float]:
            return [1.0, 0.0]

        async def fake_docs(**kwargs):
            return []

        async def fake_faqs(**kwargs):
            nonlocal seen_languages
            seen_languages = kwargs["doc_languages"]
            return []

        with (
            patch.object(docs_search, "_get_query_embedding", side_effect=fake_embedding),
            patch.object(docs_search, "_fetch_candidate_docs", side_effect=fake_docs),
            patch.object(docs_search, "_fetch_candidate_faqs", side_effect=fake_faqs),
        ):
            await docs_search.run_docs_search("como transformo?", language="es")

        self.assertEqual(seen_languages, ("es", "en"))


if __name__ == "__main__":
    unittest.main()
