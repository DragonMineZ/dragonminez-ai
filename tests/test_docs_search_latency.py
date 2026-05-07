import asyncio
import os
import unittest
from unittest.mock import patch


os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.utils import docs_search


class DocsSearchLatencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetches_embedding_and_candidates_concurrently(self) -> None:
        embedding_started = asyncio.Event()
        candidates_started = asyncio.Event()

        async def fake_embedding(query: str) -> list[float]:
            embedding_started.set()
            await asyncio.wait_for(candidates_started.wait(), timeout=0.5)
            return [1.0, 0.0]

        async def fake_candidates(**kwargs):
            candidates_started.set()
            await asyncio.wait_for(embedding_started.wait(), timeout=0.5)
            return []

        with (
            patch.object(docs_search, "_get_query_embedding", side_effect=fake_embedding),
            patch.object(docs_search, "_fetch_candidate_docs", side_effect=fake_candidates),
        ):
            result = await asyncio.wait_for(
                docs_search.run_docs_search("how do I transform?", language="en"),
                timeout=1.0,
            )

        self.assertEqual(result["matches"], [])


if __name__ == "__main__":
    unittest.main()
