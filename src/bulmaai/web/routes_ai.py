"""AI quality page: the ai_daily numbers (answers, tickets, ratings) per UTC day over 14/30/90 days, what waits on
staff, and the support eval runs found on this server."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

from bulmaai.services import ai_daily, evals
from bulmaai.web.core import BOT, Actor, api_error, requires

routes = web.RouteTableDef()

RANGES = {"14": 14, "30": 30, "90": 90}
MAX_EVAL_RUNS = 20
# The bot runs from src/, evals.RUNS_DIR is relative to the repo root.
RUNS_DIR = Path(__file__).resolve().parents[3] / evals.RUNS_DIR
RATE_FIELDS = (
    "pass",
    "score",
    "correct_full",
    "grounded",
    "handoff_ok",
    "handoff_unneeded",
    "handoff_missing",
    "language_ok",
)


def _rate(value: Any) -> float:
    return round(float(value or 0), 4)


def eval_runs(directory: Path = RUNS_DIR, limit: int = MAX_EVAL_RUNS) -> list[dict[str, Any]]:
    """The newest `limit` answers runs in the directory, newest first. A missing directory gives []; a file that
    can't be read or isn't a run is skipped. Blocking: call it in a thread."""
    runs: list[dict[str, Any]] = []
    try:
        files = evals.run_files("answers", directory)
    except OSError:
        return runs
    for path in reversed(files):
        if len(runs) >= limit:
            break
        try:
            run = evals.load_run(path)
            overall = run["metrics"]["overall"]
            cost = run.get("cost_usd")
            model = (run.get("config") or {}).get("model")
            runs.append(
                {
                    "at": datetime.fromisoformat(run["created_at"]).astimezone(timezone.utc).isoformat(),
                    "n": int(overall.get("n") or 0),
                    **{field: _rate(overall.get(field)) for field in RATE_FIELDS},
                    "cost_usd": None if cost is None else round(float(cost), 4),
                    "model": None if model is None else str(model),
                }
            )
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
    return runs


def point_json(point: ai_daily.DayPoint) -> dict[str, Any]:
    return {
        "day": point.day.isoformat(),
        "answers": dict(point.answers),
        "escalated": point.escalated,
        "tickets_opened": point.tickets_opened,
        "tickets_closed": point.tickets_closed,
        "tickets_resolved": point.tickets_resolved,
        "helpful": point.helpful,
        "not_helpful": point.not_helpful,
        "cost": round(point.cost_total, 6),
    }


@routes.get("/api/ai/quality")
@requires("status.view")
async def quality(request: web.Request, actor: Actor) -> web.Response:
    raw = request.query.get("days", "14").strip() or "14"
    if raw not in RANGES:
        raise api_error(400, "days must be 14, 30 or 90.")
    days = RANGES[raw]
    now = datetime.now(timezone.utc)
    data = await ai_daily.collect(now.date(), request.app[BOT].settings, days=days, now=now)
    runs = await asyncio.to_thread(eval_runs)
    return web.json_response(
        {
            "now": now.isoformat(),
            "from": data.points[0].day.isoformat(),
            "to": data.points[-1].day.isoformat(),
            "days": days,
            "points": [point_json(point) for point in data.points],
            "today": {
                "answers": data.today.answers_total,
                "avg7": data.answers_avg7,
                "delta": data.answers_delta,
            },
            "waiting": {"suggestions": data.suggestions_waiting, "answers_unreviewed": data.answers_unreviewed},
            "evals": runs,
        }
    )
