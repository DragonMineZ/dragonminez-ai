import json
import tempfile
import unittest
from pathlib import Path

from bulmaai.services.support_traces import (
    SupportAITrace,
    SupportSession,
    get_support_session,
    record_support_ai_trace,
    support_trace_to_eval_row,
    upsert_support_session,
    write_eval_jsonl,
)


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeConnection:
    def __init__(self) -> None:
        self.fetchrow_calls = []
        self.execute_calls = []
        self.fetchrow_result = None

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def fetchrow(self, sql: str, *args):
        self.fetchrow_calls.append((sql, args))
        return self.fetchrow_result

    async def execute(self, sql: str, *args):
        self.execute_calls.append((sql, args))
        return "OK"


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


class SupportTraceTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_support_session_maps_row(self) -> None:
        conn = FakeConnection()
        conn.fetchrow_result = {
            "channel_id": 456,
            "openai_conversation_id": "conv_123",
            "last_response_id": "resp_123",
        }

        session = await get_support_session(456, pool=FakePool(conn))

        self.assertEqual(
            session,
            SupportSession(
                channel_id=456,
                openai_conversation_id="conv_123",
                last_response_id="resp_123",
            ),
        )
        self.assertIn("FROM support_sessions", conn.fetchrow_calls[0][0])

    async def test_upsert_support_session_writes_conversation_and_last_response(self) -> None:
        conn = FakeConnection()

        await upsert_support_session(
            channel_id=456,
            openai_conversation_id="conv_123",
            last_response_id="resp_123",
            pool=FakePool(conn),
        )

        sql, args = conn.execute_calls[0]
        self.assertIn("INSERT INTO support_sessions", sql)
        self.assertIn("ON CONFLICT (channel_id)", sql)
        self.assertEqual(args, (456, "conv_123", "resp_123"))

    async def test_record_support_ai_trace_writes_usage_and_eval_payload(self) -> None:
        conn = FakeConnection()
        trace = SupportAITrace(
            workflow="support_question",
            response_id="resp_123",
            openai_conversation_id="conv_123",
            previous_response_id=None,
            model="gpt-5-mini",
            language="en",
            channel_id=456,
            user_id=123,
            prompt_cache_key="support:gpt-5-mini:en:abc",
            file_search_enabled=True,
            vector_store_ids=["vs_docs"],
            tool_names=["file_search"],
            latency_ms=1234,
            input_tokens=1200,
            output_tokens=40,
            total_tokens=1240,
            cached_tokens=900,
            reasoning_tokens=12,
            reply_text="Use the configured form key.",
            input_json=[{"role": "user", "content": "How do I transform?"}],
            request_metadata={"workflow": "support_question"},
        )

        await record_support_ai_trace(trace, pool=FakePool(conn))

        sql, args = conn.execute_calls[0]
        self.assertIn("INSERT INTO support_ai_traces", sql)
        self.assertEqual(args[0], "support_question")
        self.assertEqual(args[1], "resp_123")
        self.assertEqual(args[10], ["vs_docs"])
        self.assertEqual(args[11], ["file_search"])
        self.assertEqual(json.loads(args[19]), [{"role": "user", "content": "How do I transform?"}])
        self.assertEqual(json.loads(args[20]), {"workflow": "support_question"})

    def test_support_trace_to_eval_row_and_jsonl_export(self) -> None:
        row = {
            "id": 7,
            "created_at": "2026-05-08T12:00:00Z",
            "response_id": "resp_123",
            "model": "gpt-5-mini",
            "language": "en",
            "channel_id": 456,
            "user_id": 123,
            "tool_names": ["file_search"],
            "reply_text": "Use the configured form key.",
            "input_json": [{"role": "user", "content": "How do I transform?"}],
        }

        eval_row = support_trace_to_eval_row(row)

        self.assertEqual(eval_row["custom_id"], "support-trace-7")
        self.assertEqual(eval_row["metadata"]["response_id"], "resp_123")
        self.assertEqual(eval_row["input"][0]["content"], "How do I transform?")
        self.assertEqual(eval_row["ideal"], "Use the configured form key.")

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "eval.jsonl"
            write_eval_jsonl([eval_row], output_path)
            lines = output_path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["custom_id"], "support-trace-7")


if __name__ == "__main__":
    unittest.main()
