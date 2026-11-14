import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord
from aiohttp.test_utils import TestClient, TestServer

from bulmaai.services import ai_feedback as feedback_store
from bulmaai.services import eval_cases
from bulmaai.services import support_info as db
from bulmaai.services import support_traces
from bulmaai.web.core import SESSION_COOKIE, sign_session
from bulmaai.web.server import create_app
from test_admin_panel import HELPER_ID, RANDOM_ID, SECRET, make_bot

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc)


def answer(**overrides) -> feedback_store.AnswerToFix:
    values = dict(
        message_id=55,
        channel_id=900,
        language="es",
        created_at=NOW - timedelta(days=2),
        question="How do I turn off Ki blast griefing?",
        reply="Set `kiGriefing` to false in general-server.json.",
    )
    values.update(overrides)
    return feedback_store.AnswerToFix(**values)


def feedback_row(**overrides) -> feedback_store.FeedbackRow:
    values = dict(
        message_id=55,
        channel_id=900,
        requester_id=42,
        response_id="msg_1",
        helpful=False,
        language="es",
        created_at=NOW - timedelta(days=2),
    )
    values.update(overrides)
    return feedback_store.FeedbackRow(**values)


def info_entry(**overrides) -> db.SupportInfo:
    values = dict(
        id=12,
        kind="permanent",
        title="Ki blast griefing",
        body="Set kiGriefing to false.",
        status="active",
        source="feedback",
        ticket_id=None,
        expires_at=None,
        created_by=300,
        updated_by=300,
        review_channel_id=None,
        review_message_id=None,
        created_at=NOW,
        updated_at=NOW,
    )
    values.update(overrides)
    return db.SupportInfo(**values)


def trace(response_id="msg_1", **overrides) -> dict:
    values = {
        "id": 3,
        "created_at": NOW,
        "response_id": response_id,
        "workflow": "support_question",
        "language": "es",
        "channel_id": 900,
        "user_id": 42,
        "reply_text": "Set it to false.",
        "input_json": [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "[requester Goku · 5m ago] How do I stop griefing?"},
        ],
    }
    values.update(overrides)
    return values


def fake_pool(**methods) -> MagicMock:
    pool = MagicMock()
    for name, value in methods.items():
        setattr(pool, name, AsyncMock(return_value=value))
    return pool


# ---- services ----------------------------------------------------------------------------------------------------


class TraceLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_looks_up_sent_replies_only_and_decodes_the_input(self) -> None:
        conn = MagicMock()
        stored = {**trace(), "input_json": json.dumps(trace()["input_json"])}  # asyncpg hands jsonb back as text
        conn.fetch = AsyncMock(return_value=[stored])
        pool = MagicMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        found = await support_traces.get_reply_traces(["msg_1", "msg_1", None, "msg_2"], pool=pool)
        query, ids = conn.fetch.await_args.args
        self.assertIn("workflow IN ('support_question', 'support_escalation')", query)
        self.assertIn("response_id = ANY($1::text[])", query)
        self.assertIn("DISTINCT ON (response_id)", query)
        self.assertEqual(ids, ["msg_1", "msg_2"])
        self.assertEqual(list(found), ["msg_1"])
        self.assertEqual(found["msg_1"]["input_json"][1]["role"], "user")

    async def test_no_ids_no_query(self) -> None:
        pool = MagicMock()
        self.assertEqual(await support_traces.get_reply_traces([None, ""], pool=pool), {})
        pool.acquire.assert_not_called()


class FeedbackReviewStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_downvoted_reads_the_weeks_unreviewed_thumbs_down(self) -> None:
        record = {
            "message_id": 55,
            "channel_id": 900,
            "requester_id": 42,
            "response_id": "msg_1",
            "helpful": False,
            "language": "es",
            "created_at": NOW,
        }  # a row from before the review columns still reads
        pool = fake_pool(fetch=[record])
        with patch.object(feedback_store, "get_pool", AsyncMock(return_value=pool)):
            rows = await feedback_store.downvoted(NOW - timedelta(days=7), NOW, limit=10)
        query, *args = pool.fetch.await_args.args
        for part in ("helpful = false", "response_id IS NOT NULL", "reviewed_at IS NULL", "ORDER BY created_at DESC LIMIT $3"):
            self.assertIn(part, query)
        self.assertEqual(args, [NOW - timedelta(days=7), NOW, 10])
        self.assertEqual((rows[0].message_id, rows[0].reviewed_at, rows[0].info_id), (55, None, None))

    async def test_answers_to_fix_pairs_rows_with_their_trace(self) -> None:
        rows = [feedback_row(), feedback_row(message_id=56, response_id="msg_gone")]
        with (
            patch.object(feedback_store, "downvoted", AsyncMock(return_value=rows)),
            patch.object(feedback_store, "get_reply_traces", AsyncMock(return_value={"msg_1": trace()})) as lookup,
        ):
            items = await feedback_store.answers_to_fix(NOW - timedelta(days=7), NOW)
        lookup.assert_awaited_once_with(["msg_1", "msg_gone"])
        self.assertEqual(len(items), 1)  # the answer whose trace is gone has nothing to show
        self.assertEqual(
            (items[0].message_id, items[0].question, items[0].reply, items[0].language),
            (55, "How do I stop griefing?", "Set it to false.", "es"),
        )

    async def test_claim_release_and_link(self) -> None:
        pool = fake_pool(fetchval=55, execute=None)
        with patch.object(feedback_store, "get_pool", AsyncMock(return_value=pool)):
            self.assertTrue(await feedback_store.claim_review(55, 300))
            pool.fetchval.return_value = None
            self.assertFalse(await feedback_store.claim_review(55, 301))
            await feedback_store.release_review(55, 300)
            release_query, *release_args = pool.execute.await_args.args
            await feedback_store.set_info(55, 12)
        claim_query = pool.fetchval.await_args.args[0]
        self.assertIn("SET reviewed_at = now(), reviewed_by = $2", claim_query)
        self.assertIn("WHERE message_id = $1 AND reviewed_at IS NULL", claim_query)
        self.assertIn("reviewed_by = $2 AND info_id IS NULL", release_query)  # only your own, unfinished claim
        self.assertEqual(release_args, [55, 300])
        self.assertEqual(pool.execute.await_args.args[1:], (55, 12))

    async def test_recent_answers_to_fix_reads_the_last_30_days(self) -> None:
        with patch.object(feedback_store, "answers_to_fix", AsyncMock(return_value=[answer()])) as listed:
            items = await feedback_store.recent_answers_to_fix(NOW)
        listed.assert_awaited_once_with(NOW - timedelta(days=30), NOW, limit=50)
        self.assertEqual(items, [answer()])


# ---- panel API ---------------------------------------------------------------------------------------------------


class AnswersToFixApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = make_bot()
        dm = MagicMock(spec=discord.DMChannel)
        self.bot.get_channel = {900: SimpleNamespace(id=900, name="support"), 901: dm}.get
        self.client = TestClient(TestServer(create_app(self.bot)))
        await self.client.start_server()
        self.origin = {"Origin": f"http://{self.client.host}:{self.client.port}"}
        self.claim = AsyncMock(return_value=True)
        self.release = AsyncMock()
        self.set_info = AsyncMock()
        self.audit = AsyncMock()
        for target, replacement in (
            ("bulmaai.web.core.get_pool", AsyncMock()),
            ("bulmaai.web.routes_ai.audit", self.audit),
            ("bulmaai.web.routes_ai.feedback_store.get", AsyncMock(side_effect=self.row_for)),
            ("bulmaai.web.routes_ai.feedback_store.claim_review", self.claim),
            ("bulmaai.web.routes_ai.feedback_store.release_review", self.release),
            ("bulmaai.web.routes_ai.feedback_store.set_info", self.set_info),
        ):
            patcher = patch(target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        await self.client.close()

    @staticmethod
    async def row_for(message_id):
        return feedback_row(message_id=message_id) if message_id in (55, 56) else None

    def login(self, user_id=HELPER_ID):
        self.client.session.cookie_jar.update_cookies({SESSION_COOKIE: sign_session(SECRET, user_id)})

    async def write(self, body, message_id="55", *, add=None):
        add = add or AsyncMock(return_value=info_entry())
        with (
            patch.object(db, "add_info", add),
            patch.object(eval_cases, "spawn") as spawn,
            patch.object(eval_cases, "track_feedback", MagicMock(return_value="fb")) as track,
        ):
            response = await self.client.post(
                f"/api/ai/answers-to-fix/{message_id}/write", json=body, headers=self.origin
            )
        return response, add, spawn, track

    async def test_list_shapes_where_and_truncates(self):
        self.login()
        items = [
            answer(question="q" * 2500),
            answer(message_id=56, channel_id=901),
            answer(message_id=57, channel_id=999),
        ]
        with patch("bulmaai.web.routes_ai.feedback_store.recent_answers_to_fix", AsyncMock(return_value=items)):
            response = await self.client.get("/api/ai/answers-to-fix")
        self.assertEqual(response.status, 200, await response.text())
        answers = (await response.json())["answers"]
        self.assertEqual([item["where"] for item in answers], ["#support", "DM", None])
        self.assertEqual(
            {key: value for key, value in answers[0].items() if key != "question"},
            {
                "message_id": "55",
                "where": "#support",
                "language": "es",
                "created_at": (NOW - timedelta(days=2)).isoformat(),
                "reply": "Set `kiGriefing` to false in general-server.json.",
            },
        )
        self.assertEqual(len(answers[0]["question"]), 2000)

    async def test_write_saves_live_support_info_and_tracks_it(self):
        self.login()
        response, add, spawn, track = await self.write(
            {"title": "Ki griefing", "answer": "Set kiGriefing to false.", "kind": "temporary", "days": 3}
        )
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(await response.json(), {"info_id": 12, "title": "Ki blast griefing"})
        self.claim.assert_awaited_once_with(55, HELPER_ID)
        kwargs = add.await_args.kwargs
        self.assertEqual(
            {key: kwargs[key] for key in ("kind", "title", "body", "created_by", "status", "source")},
            {
                "kind": "temporary",
                "title": "Ki griefing",
                "body": "Set kiGriefing to false.",
                "created_by": HELPER_ID,
                "status": "active",
                "source": "feedback",
            },
        )
        left = kwargs["expires_at"] - datetime.now(timezone.utc)
        self.assertAlmostEqual(left.total_seconds(), timedelta(days=3).total_seconds(), delta=60)
        self.set_info.assert_awaited_once_with(55, 12)
        track.assert_called_once_with(55, "Set kiGriefing to false.")
        spawn.assert_called_once_with("fb")
        self.release.assert_not_awaited()
        self.assertEqual(self.audit.await_args.args[1:3], ("ai_feedback.write", "55"))

    async def test_permanent_ignores_days(self):
        self.login()
        response, add, _, _ = await self.write({"title": "T", "answer": "A", "kind": "permanent", "days": 3})
        self.assertEqual(response.status, 200)
        self.assertIsNone(add.await_args.kwargs["expires_at"])

    async def test_a_failed_link_still_saves(self):
        self.login()
        self.set_info.side_effect = RuntimeError("db down")
        with self.assertLogs("bulmaai.web.routes_ai", "ERROR"):
            response, _, spawn, _ = await self.write({"title": "T", "answer": "A", "kind": "permanent"})
        self.assertEqual(response.status, 200)
        spawn.assert_called_once_with("fb")
        self.release.assert_not_awaited()

    async def test_lost_claim_is_409_and_saves_nothing(self):
        self.login()
        self.claim.return_value = False
        response, add, spawn, _ = await self.write({"title": "T", "answer": "A", "kind": "permanent", "days": None})
        self.assertEqual(response.status, 409)
        self.assertEqual(await response.json(), {"error": "Someone already handled this one."})
        add.assert_not_awaited()
        spawn.assert_not_called()

    async def test_validation_error_releases_the_claim(self):
        self.login()
        response, _, spawn, _ = await self.write(
            {"title": "", "answer": "A", "kind": "permanent"}, add=AsyncMock(side_effect=ValueError("Title is required."))
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(await response.json(), {"error": "Title is required."})
        self.release.assert_awaited_once_with(55, HELPER_ID)
        self.set_info.assert_not_awaited()
        spawn.assert_not_called()

    async def test_db_error_releases_the_claim_and_is_500(self):
        self.login()
        with self.assertLogs("bulmaai.web.routes_ai", "ERROR"):
            response, _, _, _ = await self.write(
                {"title": "T", "answer": "A", "kind": "permanent"}, add=AsyncMock(side_effect=RuntimeError("db down"))
            )
        self.assertEqual(response.status, 500)
        self.assertEqual(await response.json(), {"error": "Couldn't save that. Try again."})
        self.release.assert_awaited_once_with(55, HELPER_ID)

    async def test_bad_kind_or_days_are_refused_before_claiming(self):
        self.login()
        for body in (
            {"title": "T", "answer": "A", "kind": "forever"},
            {"title": "T", "answer": "A", "kind": "temporary", "days": 0},
            {"title": "T", "answer": "A", "kind": "temporary", "days": 366},
            {"title": "T", "answer": "A", "kind": "temporary", "days": "3"},
            {"title": "T", "answer": "A", "kind": "temporary", "days": True},
            {"answer": "A", "kind": "permanent"},
        ):
            response, add, _, _ = await self.write(body)
            self.assertEqual(response.status, 400, body)
            add.assert_not_awaited()
        self.claim.assert_not_awaited()

    async def test_unknown_or_bad_ids_are_404(self):
        self.login()
        for message_id in ("999", "abc"):
            response, _, _, _ = await self.write({"title": "T", "answer": "A", "kind": "permanent"}, message_id)
            self.assertEqual(response.status, 404, message_id)
            skip = await self.client.post(f"/api/ai/answers-to-fix/{message_id}/skip", headers=self.origin)
            self.assertEqual(skip.status, 404, message_id)
        self.claim.assert_not_awaited()

    async def test_skip_claims_and_a_second_skip_is_409(self):
        self.login()
        response = await self.client.post("/api/ai/answers-to-fix/56/skip", headers=self.origin)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"ok": True})
        self.claim.assert_awaited_once_with(56, HELPER_ID)
        self.assertEqual(self.audit.await_args.args[1:3], ("ai_feedback.skip", "56"))
        self.claim.return_value = False
        again = await self.client.post("/api/ai/answers-to-fix/56/skip", headers=self.origin)
        self.assertEqual(again.status, 409)
        self.assertEqual(await again.json(), {"error": "Someone already handled this one."})

    async def test_needs_a_staff_session(self):
        self.assertEqual((await self.client.get("/api/ai/answers-to-fix")).status, 401)
        self.login(RANDOM_ID)
        self.assertEqual((await self.client.get("/api/ai/answers-to-fix")).status, 401)
        self.claim.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
