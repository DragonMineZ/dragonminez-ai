import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from aiohttp.test_utils import TestClient, TestServer

from bulmaai.services import ai_daily
from bulmaai.web import routes_ai
from bulmaai.web.core import SESSION_COOKIE, sign_session
from bulmaai.web.server import create_app
from test_admin_panel import HELPER_ID, RANDOM_ID, SECRET, make_bot

SETTINGS = SimpleNamespace(ai_budget_cycle_day=1, ai_monthly_budget_usd=100.0)
OVERALL = {
    "n": 40,
    "errors": 0,
    "score": 0.7,
    "correct_full": 0.55,
    "grounded": 0.9,
    "handoff_ok": 0.8,
    "handoff_unneeded": 0.15,
    "handoff_missing": 0.05,
    "language_ok": 1.0,
    "pass": 0.5,
}


def write_run(directory: Path, at: datetime, kind: str = "answers", **changes) -> None:
    run = {
        "kind": kind,
        "created_at": at.isoformat(),
        "config": {"limit": None, "source": None, "model": "claude-haiku-5-5"},
        "metrics": {"overall": dict(OVERALL)},
        "cases": [],
        "cost_usd": 1.2345,
    }
    run.update(changes)
    (directory / f"{at.strftime('%Y%m%dT%H%M%SZ')}-{kind}.json").write_text(json.dumps(run), encoding="utf-8")


class EvalRunsTests(unittest.TestCase):
    def test_missing_directory_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(routes_ai.eval_runs(Path(tmp) / "nope"), [])
            self.assertEqual(routes_ai.eval_runs(Path(tmp)), [])

    def test_newest_first_capped_and_bad_files_skipped(self) -> None:
        start = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for index in range(23):
                write_run(directory, start + timedelta(hours=index))
            write_run(directory, start + timedelta(hours=30), kind="retrieval")
            (directory / "20261103T120000Z-answers.json").write_text("{not json", encoding="utf-8")
            (directory / "20261103T130000Z-answers.json").write_text("[1, 2]", encoding="utf-8")
            (directory / "20261103T140000Z-answers.json").write_text('{"created_at": "x"}', encoding="utf-8")
            write_run(directory, start + timedelta(hours=60), cost_usd=None, config=None, metrics={"overall": {"n": 3}})

            runs = routes_ai.eval_runs(directory)
            self.assertEqual(len(routes_ai.eval_runs(directory, limit=5)), 5)

        self.assertEqual(len(runs), routes_ai.MAX_EVAL_RUNS)
        stamps = [run["at"] for run in runs]
        self.assertEqual(stamps, sorted(stamps, reverse=True))
        self.assertEqual(stamps[0], (start + timedelta(hours=60)).isoformat())
        self.assertEqual(stamps[1], (start + timedelta(hours=22)).isoformat())
        # A run without cost, config or graded cases still shows.
        self.assertEqual((runs[0]["n"], runs[0]["pass"], runs[0]["cost_usd"], runs[0]["model"]), (3, 0.0, None, None))
        self.assertEqual(
            runs[1],
            {
                "at": (start + timedelta(hours=22)).isoformat(),
                "n": 40,
                "pass": 0.5,
                "score": 0.7,
                "correct_full": 0.55,
                "grounded": 0.9,
                "handoff_ok": 0.8,
                "handoff_unneeded": 0.15,
                "handoff_missing": 0.05,
                "language_ok": 1.0,
                "cost_usd": 1.2345,
                "model": "claude-haiku-5-5",
            },
        )

    def test_runs_directory_is_the_repo_data_folder(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        self.assertEqual(routes_ai.RUNS_DIR, repo / "data" / "evals" / "runs")


class QualityApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = make_bot()
        self.client = TestClient(TestServer(create_app(self.bot)))
        await self.client.start_server()
        self.collected = []

        async def collect(day, settings, *, days=ai_daily.WINDOW_DAYS, now=None, pool=None):
            self.collected.append((day, days, now))
            return ai_daily.aggregate(
                day,
                SETTINGS,
                answer_rows=[
                    dict(day=day, workflow="support_question", reply_kind="answer", n=6),
                    dict(day=day, workflow="support_escalation", reply_kind="handoff", n=2),
                    dict(day=day - timedelta(days=1), workflow="support_question", reply_kind="clarify", n=4),
                ],
                cost_rows=[
                    dict(day=day, feature="support_question", cost=0.25),
                    dict(day=day, feature="ticket_summary", cost=0.5),
                ],
                opened_rows=[dict(day=day, n=3)],
                closed_rows=[dict(day=day, closed=2, resolved=1)],
                feedback_rows=[dict(day=day, helpful=True, n=5), dict(day=day, helpful=False, n=1)],
                suggestions_waiting=2,
                answers_unreviewed=3,
                partial=True,
                days=days,
            )

        runs = [{"at": "2026-11-12T10:00:00+00:00", "n": 40, "pass": 0.5}]
        for target, replacement in (
            ("bulmaai.web.core.get_pool", AsyncMock()),
            ("bulmaai.web.routes_ai.ai_daily.collect", collect),
            ("bulmaai.web.routes_ai.eval_runs", lambda: runs),
        ):
            patcher = patch(target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        await self.client.close()

    def login(self, user_id):
        self.client.session.cookie_jar.update_cookies({SESSION_COOKIE: sign_session(SECRET, user_id)})

    async def test_shape_for_helper(self):
        self.login(HELPER_ID)
        response = await self.client.get("/api/ai/quality")
        self.assertEqual(response.status, 200, await response.text())
        data = await response.json()

        today = datetime.now(timezone.utc).date()
        self.assertEqual(
            set(data), {"now", "from", "to", "days", "points", "today", "waiting", "evals"}
        )
        self.assertEqual(data["days"], 14)
        self.assertEqual(len(data["points"]), 14)
        self.assertEqual(data["to"], data["points"][-1]["day"])
        self.assertEqual(data["from"], data["points"][0]["day"])
        self.assertEqual(data["from"], (datetime.fromisoformat(data["to"]) - timedelta(days=13)).date().isoformat())
        self.assertLessEqual(abs((datetime.fromisoformat(data["to"]).date() - today).days), 1)
        self.assertEqual(
            data["points"][-1],
            {
                "day": data["to"],
                "answers": {"answer": 6, "clarify": 0, "handoff": 2, "other": 0},
                "escalated": 2,
                "tickets_opened": 3,
                "tickets_closed": 2,
                "tickets_resolved": 1,
                "helpful": 5,
                "not_helpful": 1,
                "cost": 0.75,
            },
        )
        self.assertEqual(data["points"][-2]["answers"]["clarify"], 4)
        self.assertEqual(data["points"][0]["answers"], {"answer": 0, "clarify": 0, "handoff": 0, "other": 0})
        self.assertEqual(data["today"]["answers"], 8)
        self.assertAlmostEqual(data["today"]["avg7"], 4 / 7)
        self.assertAlmostEqual(data["today"]["delta"], 13.0)
        self.assertEqual(data["waiting"], {"suggestions": 2, "answers_unreviewed": 3})
        self.assertEqual(data["evals"], [{"at": "2026-11-12T10:00:00+00:00", "n": 40, "pass": 0.5}])
        day, days, now = self.collected[-1]
        self.assertEqual((day, days), (now.date(), 14))
        self.assertEqual(now.utcoffset(), timedelta(0))

    async def test_days_parameter(self):
        self.login(HELPER_ID)
        for days in (30, 90):
            data = await (await self.client.get(f"/api/ai/quality?days={days}")).json()
            self.assertEqual((data["days"], len(data["points"])), (days, days))
            self.assertEqual(self.collected[-1][1], days)
        for bad in ("7", "15", "abc", "-14"):
            response = await self.client.get(f"/api/ai/quality?days={bad}")
            self.assertEqual(response.status, 400, bad)
            self.assertIn("days", (await response.json())["error"])
        self.assertEqual(len(self.collected), 2)

    async def test_no_average_gives_nulls(self):
        async def collect(day, settings, *, days=ai_daily.WINDOW_DAYS, now=None, pool=None):
            return ai_daily.aggregate(day, SETTINGS, days=days)

        self.login(HELPER_ID)
        with patch("bulmaai.web.routes_ai.ai_daily.collect", collect):
            data = await (await self.client.get("/api/ai/quality")).json()
        self.assertEqual(data["today"], {"answers": 0, "avg7": None, "delta": None})

    async def test_refused_without_status_view(self):
        self.assertEqual((await self.client.get("/api/ai/quality")).status, 401)
        # status.view is the lowest staff tier's: a member without any staff tier has no panel session at all.
        self.login(RANDOM_ID)
        self.assertEqual((await self.client.get("/api/ai/quality")).status, 401)
        self.assertEqual(self.collected, [])


if __name__ == "__main__":
    unittest.main()
