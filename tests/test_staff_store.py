import os

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import hashlib
import unittest
from datetime import datetime, timezone
from pathlib import Path

from bulmaai.services import staff_store

NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakePool:
    """Records every query; also acts as the connection. Keeps a tiny token table for issue/lookup."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.tokens: dict[str, int] = {}
        self.fetchrow_result = None
        self.fetch_result: list = []
        self.execute_status = "UPDATE 1"

    def acquire(self):
        pool = self

        class _Acquire:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *exc):
                return False

        return _Acquire()

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, sql: str, *args):
        self.calls.append((sql, args))
        if "INSERT INTO staff_mcp_tokens" in sql:
            self.tokens = {h: u for h, u in self.tokens.items() if u != args[0]}
            self.tokens[args[1]] = args[0]
        return self.execute_status

    async def executemany(self, sql: str, rows):
        self.calls.append((sql, tuple(rows)))

    async def fetchval(self, sql: str, *args):
        self.calls.append((sql, args))
        if "FROM staff_mcp_tokens" in sql or "UPDATE staff_mcp_tokens" in sql:
            return self.tokens.get(args[0])
        return 1

    async def fetchrow(self, sql: str, *args):
        self.calls.append((sql, args))
        return self.fetchrow_result

    async def fetch(self, sql: str, *args):
        self.calls.append((sql, args))
        return self.fetch_result


class StaffStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.pool = FakePool()
        original = staff_store.get_pool

        async def fake_get_pool():
            return self.pool

        staff_store.get_pool = fake_get_pool
        self.addCleanup(setattr, staff_store, "get_pool", original)

    async def test_token_round_trip_stores_only_the_hash(self) -> None:
        token = await staff_store.issue_token(42)

        self.assertGreaterEqual(len(token), 40)
        self.assertNotIn(token, self.pool.tokens)
        self.assertIn(hashlib.sha256(token.encode()).hexdigest(), self.pool.tokens)
        self.assertEqual(await staff_store.user_for_token(token), 42)
        self.assertIsNone(await staff_store.user_for_token("nope"))
        self.assertIsNone(await staff_store.user_for_token(""))

    async def test_new_token_replaces_the_old_one(self) -> None:
        old = await staff_store.issue_token(42)
        new = await staff_store.issue_token(42)

        self.assertNotEqual(old, new)
        self.assertIsNone(await staff_store.user_for_token(old))
        self.assertEqual(await staff_store.user_for_token(new), 42)

    async def test_vault_content_is_capped(self) -> None:
        await staff_store.store_message(1, 2, 3, 4, "x" * 5000, NOW)
        await staff_store.edit_message(1, "y" * 5000, NOW)

        self.assertEqual(len(self.pool.calls[0][1][4]), staff_store.CONTENT_MAX)
        self.assertIn("ON CONFLICT (message_id) DO NOTHING", self.pool.calls[0][0])
        self.assertEqual(len(self.pool.calls[1][1][1]), staff_store.CONTENT_MAX)

    async def test_note_and_reminder_text_are_capped(self) -> None:
        await staff_store.add_note(1, "n" * 400, "user")
        await staff_store.add_reminder(1, "r" * 600, NOW)
        await staff_store.replace_distilled(1, ["d" * 400, "  "])

        self.assertEqual(len(self.pool.calls[0][1][1]), staff_store.NOTE_MAX)
        self.assertEqual(len(self.pool.calls[1][1][1]), staff_store.REMINDER_MAX)
        delete_sql, _ = self.pool.calls[2]
        self.assertIn("source = 'distill'", delete_sql)
        _, rows = self.pool.calls[3]
        self.assertEqual(rows, ((1, "d" * staff_store.NOTE_MAX),))

    async def test_add_note_refuses_unknown_source(self) -> None:
        with self.assertRaises(ValueError):
            await staff_store.add_note(1, "hi", "model")

    async def test_claim_action_only_takes_open_unexpired_rows_of_the_owner(self) -> None:
        self.pool.fetchrow_result = {"id": 7, "user_id": 1, "args": '{"user_id": 5}', "outcome": "running"}

        row = await staff_store.claim_action(7, 1)

        sql, args = self.pool.calls[0]
        self.assertIn("outcome = 'running'", sql)
        self.assertIn("user_id = $2", sql)
        self.assertIn("outcome IS NULL", sql)
        self.assertIn("expires_at > now()", sql)
        self.assertEqual(args, (7, 1))
        self.assertEqual(row["args"], {"user_id": 5})

    async def test_release_only_from_running(self) -> None:
        await staff_store.release_action(7)
        self.assertIn("outcome = 'running'", self.pool.calls[0][0])

    async def test_expire_actions_returns_the_expired_rows(self) -> None:
        self.pool.fetch_result = [{"id": 3, "args": {"a": 1}}]

        rows = await staff_store.expire_actions(NOW)

        sql, args = self.pool.calls[0]
        self.assertIn("outcome = 'expired'", sql)
        self.assertIn("outcome IS NULL AND expires_at <= $1", sql)
        self.assertIn("RETURNING", sql)
        self.assertEqual(args, (NOW,))
        self.assertEqual(rows, [{"id": 3, "args": {"a": 1}}])

    async def test_claim_reminder_first_wins(self) -> None:
        self.assertTrue(await staff_store.claim_reminder(9))
        self.assertIn("sent_at IS NULL", self.pool.calls[0][0])
        self.pool.execute_status = "UPDATE 0"
        self.assertFalse(await staff_store.claim_reminder(9))

    async def test_forget_deletes_from_every_staff_table(self) -> None:
        await staff_store.forget(42)

        tables = {sql.split("FROM ")[1].split()[0] for sql, _ in self.pool.calls}
        self.assertEqual(
            tables,
            {"staff_profiles", "staff_vault", "staff_memory", "staff_reminders", "staff_actions", "staff_mcp_tokens"},
        )
        actions_sql = next(sql for sql, _ in self.pool.calls if "staff_actions" in sql)
        self.assertIn("outcome IS NULL", actions_sql)
        self.assertTrue(all(args == (42,) for _, args in self.pool.calls))

    async def test_create_action_refuses_unknown_origin(self) -> None:
        with self.assertRaises(ValueError):
            await staff_store.create_action(1, 2, "warn", {}, "Warn", "web")


def test_schema_has_every_staff_table():
    schema = (Path(__file__).resolve().parents[1] / "scripts" / "schema.sql").read_text(encoding="utf-8")
    for table in ("staff_profiles", "staff_vault", "staff_memory", "staff_reminders", "staff_actions", "staff_mcp_tokens"):
        assert f"CREATE TABLE IF NOT EXISTS {table} (" in schema
    assert "to_tsvector('simple', content)) STORED" in schema
