"""Numbers for the daily "AI metrics" staff post: the last 14 UTC days of support answers, AI spend, tickets and
answer ratings, plus the budget cycle and what waits on staff. Data only; the cog decides when and where to post."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from bulmaai.database.db import get_pool
from bulmaai.services import ai_budget, ai_usage
from bulmaai.services.support_report import SENT_WORKFLOWS

WINDOW_DAYS = 14
POST_AFTER = time(0, 10)  # UTC; the post for a day goes out once the next day reaches this time
ANSWER_KINDS = ("answer", "clarify", "handoff", "other")
COST_GROUPS = ("support", "tickets", "staff", "other")
UNREVIEWED_DAYS = 30


def cost_group(feature: str) -> str:
    """An ai_usage feature name -> one of COST_GROUPS."""
    name = str(feature or "")
    if name.startswith(("support_", "log_diagnosis")) or name == "image_reading":
        return "support"
    if name.startswith("ticket_"):
        return "tickets"
    if name.startswith("staff_"):
        return "staff"
    return "other"


@dataclass(frozen=True)
class DayPoint:
    day: date
    answers: dict[str, int]  # every key of ANSWER_KINDS
    escalated: int  # sent answers that needed a second opinion
    cost: dict[str, float]  # every key of COST_GROUPS, USD
    tickets_opened: int
    tickets_closed: int
    tickets_resolved: int
    helpful: int  # thumbs up on answers sent that day
    not_helpful: int

    @property
    def answers_total(self) -> int:
        return sum(self.answers.values())

    @property
    def cost_total(self) -> float:
        return sum(self.cost.values())


@dataclass(frozen=True)
class DailyData:
    day: date  # the reported UTC day
    points: list[DayPoint]  # consecutive days ending at `day` (WINDOW_DAYS unless asked otherwise), oldest first
    partial: bool  # `day` is today (not finished yet)
    cycle_start: date
    cycle_end: date  # exclusive, as ai_usage.cycle_bounds returns it
    cycle_spend: float  # USD from cycle_start through `day`
    monthly_budget: float
    pool_used: dict[str, int]  # tokens used on `day`: "small" and "big"
    pool_limit: dict[str, int | None]
    suggestions_waiting: int
    answers_unreviewed: int

    @property
    def today(self) -> DayPoint:
        return self.points[-1]

    @property
    def answers_avg7(self) -> float | None:
        """Mean answers per day over the 7 days before `day`; None when there were none."""
        totals = [point.answers_total for point in self.points[-8:-1]]
        return sum(totals) / len(totals) if any(totals) else None

    @property
    def answers_delta(self) -> float | None:
        average = self.answers_avg7
        return None if average is None else (self.today.answers_total - average) / average

    @property
    def projected_spend(self) -> float | None:
        elapsed = (self.day - self.cycle_start).days + 1
        if elapsed <= 0:
            return None
        return self.cycle_spend / elapsed * (self.cycle_end - self.cycle_start).days

    @property
    def has_activity(self) -> bool:
        today = self.today
        return bool(today.answers_total or today.cost_total or today.tickets_opened or today.tickets_closed)


def _get(row: Any, key: str, default: Any = None) -> Any:
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def aggregate(
    day: date,
    settings: Any,
    *,
    answer_rows: list[Any] = (),
    cost_rows: list[Any] = (),
    opened_rows: list[Any] = (),
    closed_rows: list[Any] = (),
    feedback_rows: list[Any] = (),
    pool_rows: list[Any] = (),
    cycle_spend: Any = 0,
    suggestions_waiting: Any = 0,
    answers_unreviewed: Any = 0,
    partial: bool = False,
    days: int = WINDOW_DAYS,
) -> DailyData:
    """Rows are the grouped query results of `collect` (mappings); days outside the window are ignored.
    answer_rows: day, workflow, reply_kind, n. cost_rows: day, feature, cost. opened_rows: day, n.
    closed_rows: day, closed, resolved. feedback_rows: day, helpful, n. pool_rows (of `day`): model, tokens."""
    window = ai_usage.days(day - timedelta(days=days - 1), day)
    slots: dict[date, dict[str, Any]] = {
        each: {
            "answers": dict.fromkeys(ANSWER_KINDS, 0),
            "cost": dict.fromkeys(COST_GROUPS, 0.0),
            "escalated": 0,
            "tickets_opened": 0,
            "tickets_closed": 0,
            "tickets_resolved": 0,
            "helpful": 0,
            "not_helpful": 0,
        }
        for each in window
    }

    def slot(row: Any) -> dict[str, Any] | None:
        return slots.get(_get(row, "day"))

    for row in answer_rows:
        if (found := slot(row)) is None:
            continue
        count = int(_get(row, "n", 0))
        kind = _get(row, "reply_kind", "")
        found["answers"][kind if kind in ANSWER_KINDS else "other"] += count
        if _get(row, "workflow", "") == "support_escalation":
            found["escalated"] += count
    for row in cost_rows:
        if (found := slot(row)) is not None:
            found["cost"][cost_group(_get(row, "feature", ""))] += float(_get(row, "cost", 0))
    for row in opened_rows:
        if (found := slot(row)) is not None:
            found["tickets_opened"] += int(_get(row, "n", 0))
    for row in closed_rows:
        if (found := slot(row)) is not None:
            found["tickets_closed"] += int(_get(row, "closed", 0))
            found["tickets_resolved"] += int(_get(row, "resolved", 0))
    for row in feedback_rows:
        if (found := slot(row)) is None:
            continue
        if _get(row, "helpful") is True:
            found["helpful"] += int(_get(row, "n", 0))
        elif _get(row, "helpful") is False:
            found["not_helpful"] += int(_get(row, "n", 0))

    pool_used = {ai_budget.SMALL: 0, ai_budget.BIG: 0}
    for row in pool_rows:
        pool_used[ai_budget.pool_for(str(_get(row, "model", "")))] += int(_get(row, "tokens", 0))

    cycle_start, cycle_end = ai_usage.cycle_bounds(day, getattr(settings, "ai_budget_cycle_day", 1))
    return DailyData(
        day=day,
        points=[DayPoint(day=each, **slots[each]) for each in window],
        partial=partial,
        cycle_start=cycle_start,
        cycle_end=cycle_end,
        cycle_spend=float(cycle_spend or 0),
        monthly_budget=float(getattr(settings, "ai_monthly_budget_usd", 0) or 0),
        pool_used=pool_used,
        pool_limit={
            pool: getattr(settings, f"ai_daily_{pool}_token_limit", None) for pool in (ai_budget.SMALL, ai_budget.BIG)
        },
        suggestions_waiting=int(suggestions_waiting or 0),
        answers_unreviewed=int(answers_unreviewed or 0),
    )


async def collect(
    day: date, settings: Any, *, pool: Any | None = None, now: datetime | None = None, days: int = WINDOW_DAYS
) -> DailyData:
    await ai_usage.flush()  # calls still queued in memory count too
    resolved_pool = pool or await get_pool()
    since = ai_usage.utc_midnight(day - timedelta(days=days - 1))
    day_start = ai_usage.utc_midnight(day)
    until = ai_usage.utc_midnight(day + timedelta(days=1))
    cycle_start, _ = ai_usage.cycle_bounds(day, getattr(settings, "ai_budget_cycle_day", 1))

    answers = await resolved_pool.fetch(
        """
        SELECT (created_at AT TIME ZONE 'UTC')::date AS day, workflow,
               request_metadata->>'reply_kind' AS reply_kind, count(*) AS n
        FROM support_ai_traces
        WHERE created_at >= $1 AND created_at < $2 AND workflow = ANY($3::text[])
        GROUP BY 1, 2, 3
        """,
        since,
        until,
        list(SENT_WORKFLOWS),
    )
    costs = await resolved_pool.fetch(
        """
        SELECT (created_at AT TIME ZONE 'UTC')::date AS day, feature, SUM(cost_usd) AS cost
        FROM ai_usage WHERE created_at >= $1 AND created_at < $2 GROUP BY 1, 2
        """,
        since,
        until,
    )
    opened = await resolved_pool.fetch(
        """
        SELECT (created_at AT TIME ZONE 'UTC')::date AS day, count(*) AS n
        FROM tickets WHERE created_at >= $1 AND created_at < $2 AND status <> 'creating' GROUP BY 1
        """,
        since,
        until,
    )
    closed = await resolved_pool.fetch(
        """
        SELECT (closed_at AT TIME ZONE 'UTC')::date AS day, count(*) AS closed,
               count(*) FILTER (WHERE resolved) AS resolved
        FROM ticket_transcripts WHERE closed_at >= $1 AND closed_at < $2 GROUP BY 1
        """,
        since,
        until,
    )
    feedback = await resolved_pool.fetch(
        """
        SELECT (created_at AT TIME ZONE 'UTC')::date AS day, helpful, count(*) AS n
        FROM ai_feedback
        WHERE created_at >= $1 AND created_at < $2 AND helpful IS NOT NULL GROUP BY 1, 2
        """,
        since,
        until,
    )
    pools = await resolved_pool.fetch(
        """
        SELECT model, SUM(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens)::BIGINT AS tokens
        FROM ai_usage WHERE created_at >= $1 AND created_at < $2 AND feature NOT LIKE 'eval%' GROUP BY model
        """,  # like the bot's own caps (ai_usage.tokens_by_model_since): offline eval runs don't count
        day_start,
        until,
    )
    cycle_spend = await resolved_pool.fetchval(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM ai_usage WHERE created_at >= $1 AND created_at < $2",
        ai_usage.utc_midnight(cycle_start),
        until,
    )
    suggestions = await resolved_pool.fetchval("SELECT count(*) FROM support_info WHERE status = 'suggested'")
    unreviewed = await resolved_pool.fetchval(
        """
        SELECT count(*) FROM ai_feedback
        WHERE helpful = FALSE AND reviewed_at IS NULL AND response_id IS NOT NULL
          AND created_at >= $1 AND created_at < $2
        """,
        until - timedelta(days=UNREVIEWED_DAYS),
        until,
    )
    return aggregate(
        day,
        settings,
        answer_rows=list(answers),
        cost_rows=list(costs),
        opened_rows=list(opened),
        closed_rows=list(closed),
        feedback_rows=list(feedback),
        pool_rows=list(pools),
        cycle_spend=cycle_spend,
        suggestions_waiting=suggestions,
        answers_unreviewed=unreviewed,
        partial=day == (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date(),
        days=days,
    )


# ---- schedule state (one ai_daily_posts row per posted day) ------------------------------------------------------


def post_due(now: datetime) -> date | None:
    """The UTC day to post: yesterday once now's UTC time reaches POST_AFTER. now must be timezone-aware."""
    now = now.astimezone(timezone.utc)
    return now.date() - timedelta(days=1) if now.time() >= POST_AFTER else None


async def claim(day: date, *, pool: Any | None = None) -> bool:
    """True only for the first caller for that day, so a second loop or a restart can't post it twice."""
    resolved_pool = pool or await get_pool()
    claimed = await resolved_pool.fetchval(
        "INSERT INTO ai_daily_posts (day) VALUES ($1) ON CONFLICT DO NOTHING RETURNING day", day
    )
    return claimed is not None


async def release(day: date, *, pool: Any | None = None) -> None:
    """Sending failed: drop the claim so the next tick retries."""
    resolved_pool = pool or await get_pool()
    await resolved_pool.execute("DELETE FROM ai_daily_posts WHERE day = $1", day)
