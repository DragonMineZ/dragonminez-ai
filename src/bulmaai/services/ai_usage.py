"""Per-call Claude usage ledger (`ai_usage` table), priced in dollars, for the panel's AI usage page.

`ai_budget.record_response` hands every call here. `record` only queues the row in memory (it must stay sync and
never raise); a background task started by the bot flushes the queue in one executemany every few seconds. Without
that task (tests, a failed DB at startup) rows just wait in a capped queue and the oldest are dropped.
"""

import asyncio
import logging
from collections import deque
from datetime import date, datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

# List prices in USD per million tokens. long_* apply when the prompt (input + cache read + cache write)
# is over LONG_PROMPT_TOKENS.
PRICES: dict[str, dict[str, float | None]] = {
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "long_input": 0.50, "long_output": 2.50},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "long_input": None, "long_output": None},
}
LONG_PROMPT_TOKENS = 100_000
CACHE_WRITE_FACTOR = 1.25
CACHE_READ_FACTOR = 0.10
SEARCH_USD = 0.01

FEATURE_LABELS: dict[str, str] = {
    "support_question": "Support answers",
    "support_escalation": "Support escalations",
    "support_draft": "Support drafts (replaced)",
    "log_diagnosis": "Crash-log diagnosis",
    "log_diagnosis_escalation": "Crash-log diagnosis escalations",
    "log_diagnosis_draft": "Crash-log diagnosis drafts (replaced)",
    "staff_draft": "Staff draft replies",
    "staff_draft_escalation": "Staff draft escalations",
    "staff_draft_draft": "Staff draft drafts (replaced)",
    "image_reading": "Screenshot reading",
    "ticket_intake": "Ticket form triage",
    "ticket_solved_check": "Ticket \"is it solved?\" check",
    "ticket_summary": "Ticket close summaries",
    "ticket_copilot_summary": "Staff copilot summaries",
    "ticket_translation": "Ticket translation (to the ticket language)",
    "ticket_owner_translation": "Ticket translation (owner to staff)",
    "announcement_translation": "Announcement translation",
    "bug_report_triage": "Bug report triage",
    "bug_report_duplicates": "Bug report duplicate check",
    "code_notes": "Code notes",
    "staff_assistant": "Staff Bulma",
    "staff_distill": "Staff Bulma memory notes",
    "weekly_report": "Weekly support report",
    "eval": "Evals: replayed answers",
    "eval_escalation": "Evals: replayed escalations",
    "eval_draft": "Evals: replayed drafts (replaced)",
    "eval_judge": "Evals: grading and labelling",
    "other": "Other",
}

MAX_QUEUE = 5000
FLUSH_SECONDS = 5
RETENTION_DAYS = 400
BACKFILL_DAYS = 90

_queue: deque[tuple] = deque()
_warned_models: set[str] = set()
_dropped_logged = False
_flusher: asyncio.Task | None = None


def _rates(model: str) -> dict[str, float | None]:
    name = (model or "").lower()
    for key, rates in PRICES.items():
        if name.startswith(key):
            return rates
    if name not in _warned_models:
        _warned_models.add(name)
        log.info("No price for AI model %r; costing it like the most expensive known model", model)
    return max(PRICES.values(), key=lambda rates: rates["output"] or 0)


def cost(model: str, input_tokens: int = 0, output_tokens: int = 0, cache_read: int = 0, cache_write: int = 0, searches: int = 0) -> float:
    """List-price cost in USD of one call."""
    rates = _rates(model)
    in_rate, out_rate = rates["input"] or 0.0, rates["output"] or 0.0
    if input_tokens + cache_read + cache_write > LONG_PROMPT_TOKENS and rates.get("long_input") is not None:
        in_rate, out_rate = rates["long_input"] or 0.0, rates["long_output"] or out_rate
    tokens_usd = (
        input_tokens * in_rate
        + output_tokens * out_rate
        + cache_write * in_rate * CACHE_WRITE_FACTOR
        + cache_read * in_rate * CACHE_READ_FACTOR
    ) / 1_000_000
    return round(tokens_usd + searches * SEARCH_USD, 6)


def price_table() -> dict[str, dict[str, float | None]]:
    return {
        model: {
            "input": rates["input"],
            "output": rates["output"],
            "cache_read": round((rates["input"] or 0) * CACHE_READ_FACTOR, 6),
            "cache_write": round((rates["input"] or 0) * CACHE_WRITE_FACTOR, 6),
            "long_input": rates["long_input"],
            "long_output": rates["long_output"],
        }
        for model, rates in PRICES.items()
    }


def _field(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _count(obj: Any, name: str) -> int:
    try:
        return max(int(_field(obj, name) or 0), 0)
    except (TypeError, ValueError):
        return 0


def record(feature: str, model: str, input_tokens: int = 0, output_tokens: int = 0, cache_read: int = 0, cache_write: int = 0, searches: int = 0) -> None:
    """Queue one call for the ledger. Never raises."""
    global _dropped_logged
    try:
        model = str(model or "")
        row = (
            datetime.now(timezone.utc),
            str(feature or "other"),
            model,
            input_tokens,
            output_tokens,
            cache_read,
            cache_write,
            searches,
            cost(model, input_tokens, output_tokens, cache_read, cache_write, searches),
        )
        if len(_queue) >= MAX_QUEUE:
            _queue.popleft()
            if not _dropped_logged:
                _dropped_logged = True
                log.info("AI usage queue is full (%s rows); dropping the oldest", MAX_QUEUE)
        _queue.append(row)
    except Exception:
        log.info("Could not queue an AI usage row", exc_info=True)


def record_response(response: Any, feature: str = "other") -> None:
    """Queue a Claude response's usage. Never raises."""
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        record(
            feature,
            str(getattr(response, "model", None) or ""),
            _count(usage, "input_tokens"),
            _count(usage, "output_tokens"),
            _count(usage, "cache_read_input_tokens"),
            _count(usage, "cache_creation_input_tokens"),
            _count(_field(usage, "server_tool_use"), "web_search_requests"),
        )
    except Exception:
        log.info("Could not read AI usage from a response", exc_info=True)


def pending() -> list[tuple]:
    return list(_queue)


def reset() -> None:
    global _dropped_logged
    _queue.clear()
    _dropped_logged = False


INSERT_SQL = """
INSERT INTO ai_usage (created_at, feature, model, input_tokens, output_tokens, cache_read_tokens,
                      cache_write_tokens, web_searches, cost_usd, backfilled)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
"""


async def flush(pool: Any | None = None) -> int:
    """Write the queued rows; on a DB failure they are dropped (logged at INFO)."""
    global _dropped_logged
    if not _queue:
        return 0
    rows = list(_queue)
    _queue.clear()
    _dropped_logged = False
    try:
        if pool is None:
            from bulmaai.database.db import get_pool

            pool = await get_pool()
        await pool.executemany(INSERT_SQL, [(*row, False) for row in rows])
    except Exception as error:
        log.info("Dropped %s AI usage rows: %s: %s", len(rows), type(error).__name__, error)
        return 0
    return len(rows)


async def _flush_loop() -> None:
    while True:
        await asyncio.sleep(FLUSH_SECONDS)
        await flush()


def start_flusher() -> None:
    global _flusher
    if _flusher is None or _flusher.done():
        _flusher = asyncio.get_running_loop().create_task(_flush_loop(), name="ai_usage_flush")


async def stop_flusher() -> None:
    global _flusher
    task, _flusher = _flusher, None
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    await flush()


async def prepare(pool: Any) -> None:
    """Startup: drop rows past retention, and backfill from support traces when the ledger is empty."""
    await pool.execute("DELETE FROM ai_usage WHERE created_at < now() - make_interval(days => $1)", RETENTION_DAYS)
    if await pool.fetchval("SELECT EXISTS (SELECT 1 FROM ai_usage)"):
        return
    # Approximate: support traces only (no other features), and cache writes are whatever the total holds beyond
    # input, output and cache reads.
    traces = await pool.fetch(
        """
        SELECT created_at, COALESCE(NULLIF(workflow, ''), 'other') AS feature, COALESCE(model, '') AS model,
               GREATEST(COALESCE(input_tokens, 0), 0) AS input_tokens,
               GREATEST(COALESCE(output_tokens, 0), 0) AS output_tokens,
               GREATEST(COALESCE(cached_tokens, 0), 0) AS cache_read,
               GREATEST(COALESCE(total_tokens, 0) - COALESCE(input_tokens, 0) - COALESCE(output_tokens, 0)
                        - COALESCE(cached_tokens, 0), 0) AS cache_write
        FROM support_ai_traces
        WHERE created_at >= now() - make_interval(days => $1)
        """,
        BACKFILL_DAYS,
    )
    rows = [
        (
            trace["created_at"],
            trace["feature"],
            trace["model"],
            trace["input_tokens"],
            trace["output_tokens"],
            trace["cache_read"],
            trace["cache_write"],
            0,
            cost(trace["model"], trace["input_tokens"], trace["output_tokens"], trace["cache_read"], trace["cache_write"]),
            True,
        )
        for trace in traces
    ]
    if rows:
        await pool.executemany(INSERT_SQL, rows)
        log.info("Backfilled %s AI usage rows from support traces", len(rows))


async def tokens_by_model_since(pool: Any, since: datetime) -> list[tuple[str, int]] | None:
    """(model, tokens) the bot spent since `since`, or None while the ledger has no row at all. Offline eval runs
    (scripts/support_eval.py, features eval*) are left out: they must not eat the bot's daily token caps."""
    if not await pool.fetchval("SELECT EXISTS (SELECT 1 FROM ai_usage)"):
        return None
    rows = await pool.fetch(
        """
        SELECT model, SUM(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens)::BIGINT AS tokens
        FROM ai_usage
        WHERE created_at >= $1 AND feature NOT LIKE 'eval%'
        GROUP BY model
        """,
        since,
    )
    return [(str(row["model"]), int(row["tokens"] or 0)) for row in rows]


def cycle_bounds(today: date, cycle_day: Any) -> tuple[date, date]:
    """[start, end) of the billing cycle holding `today`; the credit renews on `cycle_day` (clamped to 1-28)."""
    try:
        day = min(max(int(cycle_day), 1), 28)
    except (TypeError, ValueError):
        day = 1

    def shift(year: int, month: int, by: int) -> date:
        index = year * 12 + (month - 1) + by
        return date(index // 12, index % 12 + 1, day)

    start = shift(today.year, today.month, 0 if today.day >= day else -1)
    return start, shift(start.year, start.month, 1)


def utc_midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def days(start: date, end: date) -> list[date]:
    """Every day from start through end, inclusive."""
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
