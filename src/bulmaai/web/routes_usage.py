"""AI usage page: the ai_usage ledger grouped by day, feature and model, against the monthly Claude budget."""

from datetime import date, datetime, timedelta, timezone
from typing import Any

from aiohttp import web

from bulmaai.database.db import get_pool
from bulmaai.services import ai_budget, ai_usage
from bulmaai.web.core import BOT, Actor, api_error, audit, requires

routes = web.RouteTableDef()

RANGES = {"7": 7, "30": 30, "90": 90}
TOP_CALLS = 15
MAX_DAYS_BACK = ai_usage.RETENTION_DAYS

GROUP_COLUMNS = """
    COUNT(*) AS calls,
    COALESCE(SUM(input_tokens), 0)::BIGINT AS input,
    COALESCE(SUM(output_tokens), 0)::BIGINT AS output,
    COALESCE(SUM(cache_read_tokens), 0)::BIGINT AS cache_read,
    COALESCE(SUM(cache_write_tokens), 0)::BIGINT AS cache_write,
    COALESCE(SUM(web_searches), 0)::BIGINT AS searches,
    COALESCE(SUM(cost_usd), 0) AS cost
"""


def _usd(value: Any) -> float:
    return round(float(value or 0), 6)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def range_start(raw: str, today: date, cycle_start: date) -> date:
    """First day of the requested range; ValueError on anything but cycle/7/30/90."""
    if raw == "cycle":
        return cycle_start
    if raw in RANGES:
        return today - timedelta(days=RANGES[raw] - 1)
    raise ValueError(raw)


def _group_row(row: Any, **keys: Any) -> dict[str, Any]:
    return {
        **keys,
        "feature": row["feature"],
        "model": row["model"],
        "pool": ai_budget.pool_for(row["model"]),
        "calls": int(row["calls"]),
        "input": int(row["input"]),
        "output": int(row["output"]),
        "cache_read": int(row["cache_read"]),
        "cache_write": int(row["cache_write"]),
        "searches": int(row["searches"]),
        "cost": _usd(row["cost"]),
    }


@routes.get("/api/usage")
@requires("status.view")
async def usage(request: web.Request, actor: Actor) -> web.Response:
    settings = request.app[BOT].settings
    now = datetime.now(timezone.utc)
    today = now.date()
    cycle_start, cycle_end = ai_usage.cycle_bounds(today, getattr(settings, "ai_budget_cycle_day", 1))
    try:
        start = range_start(request.query.get("range", "cycle").strip() or "cycle", today, cycle_start)
    except ValueError:
        raise api_error(400, "range must be cycle, 7, 30 or 90.")
    since, until = ai_usage.utc_midnight(start), ai_usage.utc_midnight(today + timedelta(days=1))
    cycle_since = ai_usage.utc_midnight(cycle_start)

    await ai_usage.flush()
    pool = await get_pool()
    async with pool.acquire() as conn:
        grouped = await conn.fetch(
            f"""
            SELECT (created_at AT TIME ZONE 'UTC')::date AS day, feature, model, {GROUP_COLUMNS}
            FROM ai_usage
            WHERE created_at >= $1 AND created_at < $2
            GROUP BY 1, 2, 3
            ORDER BY 1, 2, 3
            """,
            since,
            until,
        )
        daily = await conn.fetch(
            """
            SELECT (created_at AT TIME ZONE 'UTC')::date AS day, COALESCE(SUM(cost_usd), 0) AS cost
            FROM ai_usage
            WHERE created_at >= $1 AND created_at < $2
            GROUP BY 1
            """,
            cycle_since,
            until,
        )
        top = await conn.fetch(
            """
            SELECT created_at, feature, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                   web_searches, cost_usd
            FROM ai_usage
            WHERE created_at >= $1 AND created_at < $2
            ORDER BY cost_usd DESC, created_at DESC
            LIMIT $3
            """,
            since,
            until,
            TOP_CALLS,
        )
        tracking_since = await conn.fetchval("SELECT MIN(created_at) FROM ai_usage WHERE NOT backfilled")
        reset_at = await ai_usage.last_budget_reset(conn)
        backfilled_until = await conn.fetchval("SELECT MAX(created_at) FROM ai_usage WHERE backfilled")

    daily_cost = {row["day"]: _usd(row["cost"]) for row in daily}
    return web.json_response(
        {
            "now": now.isoformat(),
            "budget": {
                "monthly_usd": float(getattr(settings, "ai_monthly_budget_usd", 100.0) or 0),
                "cycle_start": cycle_start.isoformat(),
                "cycle_end": cycle_end.isoformat(),
                "spent_usd": _usd(sum(daily_cost.values())),
                "days_elapsed": round((now - cycle_since).total_seconds() / 86400, 6),
                "days_total": (cycle_end - cycle_start).days,
            },
            "limits": {
                pool_name: getattr(settings, f"ai_daily_{pool_name}_token_limit", None)
                for pool_name in (ai_budget.SMALL, ai_budget.BIG)
            },
            "today_tokens": {pool_name: ai_budget.used(pool_name) for pool_name in (ai_budget.SMALL, ai_budget.BIG)},
            "limits_reset_at": _iso(reset_at) if reset_at and reset_at.astimezone(timezone.utc).date() == today else None,
            "from": start.isoformat(),
            "to": today.isoformat(),
            "rows": [_group_row(row, day=row["day"].isoformat()) for row in grouped],
            "cycle_daily": [
                {"day": day.isoformat(), "cost": daily_cost.get(day, 0.0)} for day in ai_usage.days(cycle_start, today)
            ],
            "top_calls": [
                {
                    "at": _iso(row["created_at"]),
                    "feature": row["feature"],
                    "model": row["model"],
                    "input": int(row["input_tokens"]),
                    "output": int(row["output_tokens"]),
                    "cache_read": int(row["cache_read_tokens"]),
                    "cache_write": int(row["cache_write_tokens"]),
                    "searches": int(row["web_searches"]),
                    "cost": _usd(row["cost_usd"]),
                }
                for row in top
            ],
            "prices": ai_usage.price_table(),
            "search_usd": ai_usage.SEARCH_USD,
            "feature_labels": dict(ai_usage.FEATURE_LABELS),
            "tracking_since": _iso(tracking_since),
            "backfilled_until": _iso(backfilled_until),
        }
    )


@routes.post("/api/usage/reset-limits")
@requires("settings.edit")
async def reset_limits(request: web.Request, actor: Actor) -> web.Response:
    """Zero today's token pools. The time is stored so the next startup seeds from it, not from midnight."""
    now = datetime.now(timezone.utc)
    await ai_usage.flush()
    await ai_usage.mark_budget_reset(await get_pool(), now)
    ai_budget.reset()
    await audit(actor, "ai.limits_reset")
    return web.json_response({"ok": True, "today_tokens": {ai_budget.SMALL: 0, ai_budget.BIG: 0}, "reset_at": now.isoformat()})


def parse_day(raw: str, today: date) -> date:
    """A YYYY-MM-DD date within the last MAX_DAYS_BACK days (today included); ValueError otherwise."""
    if len(raw) != 10:
        raise ValueError(raw)
    day = date.fromisoformat(raw)
    if day > today or day < today - timedelta(days=MAX_DAYS_BACK):
        raise ValueError(raw)
    return day


@routes.get("/api/usage/day")
@requires("status.view")
async def usage_day(request: web.Request, actor: Actor) -> web.Response:
    today = datetime.now(timezone.utc).date()
    try:
        day = parse_day(request.query.get("date", "").strip(), today)
    except ValueError:
        raise api_error(400, f"date must be YYYY-MM-DD within the last {MAX_DAYS_BACK} days.")
    await ai_usage.flush()
    pool = await get_pool()
    rows = await pool.fetch(
        f"""
        SELECT EXTRACT(HOUR FROM created_at AT TIME ZONE 'UTC')::int AS hour, feature, model, {GROUP_COLUMNS}
        FROM ai_usage
        WHERE created_at >= $1 AND created_at < $2
        GROUP BY 1, 2, 3
        ORDER BY 1, 2, 3
        """,
        ai_usage.utc_midnight(day),
        ai_usage.utc_midnight(day + timedelta(days=1)),
    )
    return web.json_response(
        {"date": day.isoformat(), "rows": [_group_row(row, hour=int(row["hour"])) for row in rows]}
    )
