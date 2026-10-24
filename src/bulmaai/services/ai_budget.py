"""Daily Claude token caps, so a busy day can't run up the bill.

Haiku models draw from the "small" pool, every other model from the "big" pool.
Days roll over at 00:00 UTC.
"""

import logging
from datetime import date, datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

SMALL, BIG = "small", "big"

# ponytail: in-memory, seeded from today's support traces at startup; non-support calls made
# before a restart are forgotten, the default limits leave headroom for that.
_day: date | None = None
_used: dict[str, int] = {}


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _roll() -> None:
    global _day
    today = _today()
    if _day != today:
        _day = today
        _used.clear()


def reset() -> None:
    global _day
    _day = None
    _used.clear()


def pool_for(model: str | None) -> str:
    return SMALL if "haiku" in (model or "").lower() else BIG


def record(model: Any, tokens: Any) -> None:
    """Never raises: accounting must not break a reply."""
    try:
        amount = int(tokens)
    except (TypeError, ValueError):
        return
    if amount <= 0:
        return
    _roll()
    pool = pool_for(str(model or ""))
    _used[pool] = _used.get(pool, 0) + amount


def usage_total(usage: Any) -> int | None:
    """Every token a Claude call billed: input, output and both cache counters."""
    if usage is None:
        return None
    try:
        return sum(
            int(getattr(usage, name, 0) or 0)
            for name in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        )
    except (TypeError, ValueError):
        return None


def record_response(response: Any) -> None:
    record(getattr(response, "model", None), usage_total(getattr(response, "usage", None)))


def used(pool: str) -> int:
    _roll()
    return _used.get(pool, 0)


def has_room(pool: str, settings: Any) -> bool:
    """An unset limit means unlimited; 0 disables the pool."""
    limit = getattr(settings, f"ai_daily_{pool}_token_limit", None)
    return limit is None or used(pool) < int(limit)


def is_paused(settings: Any) -> bool:
    return not has_room(SMALL, settings)


def pick_model(preferred: str, settings: Any) -> str | None:
    """Model for a call: None when the small pool is spent (AI paused),
    the small model when a big model is asked for but the big pool is spent."""
    if is_paused(settings):
        return None
    if pool_for(preferred) == BIG and not has_room(BIG, settings):
        return getattr(settings, "ai_model", preferred)
    return preferred


def escalation_routes(settings: Any) -> list[tuple[str, str, bool]]:
    """(model, effort, use_tools) second opinions, cheapest first: the small model at high effort while the
    small pool has room, then the big model while the big pool has room."""
    routes = []
    if has_room(SMALL, settings):
        routes.append((getattr(settings, "ai_model", "claude-haiku-5-5"), "high", True))
    if has_room(BIG, settings):
        routes.append(
            (getattr(settings, "ai_support_escalation_model", "claude-sonnet-5-5"), getattr(settings, "ai_support_effort", "medium"), True)
        )
    return routes


def seed(rows: Any) -> None:
    """Rows of (model, tokens) already spent today."""
    for model, tokens in rows:
        record(model, tokens)
