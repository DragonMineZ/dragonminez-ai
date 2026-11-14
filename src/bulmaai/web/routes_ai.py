"""AI quality page: the ai_daily numbers (answers, tickets, ratings) per UTC day over 14/30/90 days, what waits on
staff, the support eval runs found on this server, the "Answers to fix" review of 👎 answers (Write goes live as
support info, source feedback; Skip) and the stored weekly support reports."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import discord
from aiohttp import web

from bulmaai.services import ai_daily, eval_cases, evals
from bulmaai.services import ai_feedback as feedback_store
from bulmaai.services import support_info
from bulmaai.services import support_report as report
from bulmaai.web.core import BOT, Actor, api_error, audit, read_json, requires

log = logging.getLogger(__name__)

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
TEXT_MAX = 2000  # question / reply as sent to the page
HANDLED = "Someone already handled this one."


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


# ---- "Answers to fix" -------------------------------------------------------------------------------------------


def _where(bot: Any, channel_id: int) -> str | None:
    channel = bot.get_channel(channel_id)
    if isinstance(channel, discord.abc.PrivateChannel):
        return "DM"
    name = getattr(channel, "name", None)
    return f"#{name}" if name else None


def answer_json(item: feedback_store.AnswerToFix, where: str | None) -> dict[str, Any]:
    return {
        "message_id": str(item.message_id),
        "where": where,
        "language": item.language,
        "created_at": item.created_at.isoformat(),
        "question": item.question[:TEXT_MAX],
        "reply": item.reply[:TEXT_MAX],
    }


async def _feedback_row(request: web.Request) -> feedback_store.FeedbackRow:
    raw = request.match_info["message_id"]
    row = await feedback_store.get(int(raw)) if raw.isdigit() else None
    if row is None:
        raise api_error(404, "Unknown answer.")
    return row


def _expiry(kind: Any, days: Any) -> datetime | None:
    """expires_at for the Write form: now + days, temporary entries only; days is optional, 1-365."""
    if kind not in support_info.KINDS:
        raise api_error(400, "Kind must be permanent or temporary.")
    if days is None:
        return None
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
        raise api_error(400, "Expires in days must be a whole number from 1 to 365.")
    return datetime.now(timezone.utc) + timedelta(days=days) if kind == support_info.KIND_TEMPORARY else None


@routes.get("/api/ai/answers-to-fix")
@requires("support_info.edit")
async def answers_to_fix(request: web.Request, actor: Actor) -> web.Response:
    bot = request.app[BOT]
    items = await feedback_store.recent_answers_to_fix()
    return web.json_response({"answers": [answer_json(item, _where(bot, item.channel_id)) for item in items]})


@routes.post("/api/ai/answers-to-fix/{message_id}/write")
@requires("support_info.edit")
async def answer_write(request: web.Request, actor: Actor) -> web.Response:
    """Claims the answer (first wins), saves the text as active support info (source feedback, created by the staff
    member) and links it to the answer. A text that doesn't validate gives the claim back."""
    row = await _feedback_row(request)
    payload = await read_json(request)
    title, answer, kind = payload.get("title"), payload.get("answer"), payload.get("kind")
    if not isinstance(title, str) or not isinstance(answer, str):
        raise api_error(400, "Title and answer are required.")
    expires_at = _expiry(kind, payload.get("days"))
    message_id = row.message_id
    if not await feedback_store.claim_review(message_id, actor.id):
        raise api_error(409, HANDLED)
    try:
        entry = await support_info.add_info(
            kind=kind,
            title=title,
            body=answer,
            created_by=actor.id,
            expires_at=expires_at,
            status="active",
            source="feedback",
        )
    except ValueError as error:
        await feedback_store.release_review(message_id, actor.id)
        raise api_error(400, str(error))
    except Exception:
        log.exception("Could not save the right answer", extra={"message_id": message_id})
        await feedback_store.release_review(message_id, actor.id)
        raise api_error(500, "Couldn't save that. Try again.")
    try:
        await feedback_store.set_info(message_id, entry.id)
    except Exception:
        # The support info is live already; only the link back from the answer is missing.
        log.exception("Could not link the right answer to its feedback row", extra={"message_id": message_id})
    eval_cases.spawn(eval_cases.track_feedback(message_id, answer))
    await audit(actor, "ai_feedback.write", str(message_id), info_id=entry.id, title=entry.title)
    return web.json_response({"info_id": entry.id, "title": entry.title})


@routes.post("/api/ai/answers-to-fix/{message_id}/skip")
@requires("support_info.edit")
async def answer_skip(request: web.Request, actor: Actor) -> web.Response:
    row = await _feedback_row(request)
    if not await feedback_store.claim_review(row.message_id, actor.id):
        raise api_error(409, HANDLED)
    await audit(actor, "ai_feedback.skip", str(row.message_id))
    return web.json_response({"ok": True})


# ---- weekly support reports -------------------------------------------------------------------------------------


MAX_REPORTS = 26


def report_head(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "since": row["since"].isoformat(),
        "until": row["until"].isoformat(),
        "created_at": row["created_at"].isoformat(),
    }


@routes.get("/api/ai/reports")
@requires("status.view")
async def reports(request: web.Request, actor: Actor) -> web.Response:
    rows = await report.list_reports(MAX_REPORTS)
    return web.json_response({"reports": [report_head(row) for row in rows]})


@routes.get("/api/ai/reports/{id}")
@requires("status.view")
async def report_detail(request: web.Request, actor: Actor) -> web.Response:
    raw = request.match_info["id"]
    found = await report.get_report(int(raw)) if raw.isdigit() else None
    if found is None:
        raise api_error(404, "Unknown report.")
    return web.json_response({**report_head(found), "numbers": found["data"], "insights": found["insights"]})
