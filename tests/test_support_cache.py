import os
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch


os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.services import support_cache


class FakeConnection:
    def __init__(self) -> None:
        self.fetchval_calls = []

    async def fetchval(self, sql: str, *args):
        self.fetchval_calls.append((sql, args))
        return datetime(2026, 5, 7, tzinfo=timezone.utc)


class FakeAcquire:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn

    async def __aenter__(self) -> FakeConnection:
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.conn)


class SupportCacheVersionTests(unittest.IsolatedAsyncioTestCase):
    async def test_docs_version_includes_faq_knowledge_version_timestamp(self) -> None:
        conn = FakeConnection()

        with patch.object(support_cache, "get_pool", new=AsyncMock(return_value=FakePool(conn))):
            result = await support_cache.get_docs_version()

        self.assertEqual(result, datetime(2026, 5, 7, tzinfo=timezone.utc))
        sql = conn.fetchval_calls[0][0]
        self.assertIn("FROM docs", sql)
        self.assertIn("knowledge_base_version", sql)
        self.assertIn("faq", sql)


if __name__ == "__main__":
    unittest.main()
