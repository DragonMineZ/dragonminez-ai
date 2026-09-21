import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from bulmaai.services.dev_jar_pending_review import reset_pending_dev_jar_commits


class ResetPendingDevJarCommitsTests(unittest.IsolatedAsyncioTestCase):
    async def test_reset_issues_update_not_delete(self) -> None:
        conn = AsyncMock()
        pool = MagicMock()
        pool.acquire.return_value.__aenter__.return_value = conn

        with patch("bulmaai.services.dev_jar_pending_review.get_pool", AsyncMock(return_value=pool)):
            await reset_pending_dev_jar_commits()

        conn.execute.assert_awaited_once()
        query = conn.execute.await_args.args[0]
        self.assertIn("UPDATE dev_jar_pending_review", query)
        self.assertIn("commits = '[]'", query)
        self.assertNotIn("DELETE", query.upper())

    async def test_reset_targets_the_singleton_row(self) -> None:
        conn = AsyncMock()
        pool = MagicMock()
        pool.acquire.return_value.__aenter__.return_value = conn

        with patch("bulmaai.services.dev_jar_pending_review.get_pool", AsyncMock(return_value=pool)):
            await reset_pending_dev_jar_commits()

        query = conn.execute.await_args.args[0]
        self.assertIn("WHERE id = 1", query)


if __name__ == "__main__":
    unittest.main()
