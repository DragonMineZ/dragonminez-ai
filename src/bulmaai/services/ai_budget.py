"""Daily OpenAI token budget for the free data-sharing pools.

Tool-free traffic on mini/nano models draws from the "small" pool, other models from the
"big" pool. Requests with tools are excluded from the free program, so they draw from a
"billed" pool we cap ourselves. Days roll over at 00:00 UTC, like OpenAI's pools.
"""

import logging
from datetime import date, datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

SMALL, BIG, BILLED = "small", "big", "billed"

# ponytail: in-memory, seeded from today's support traces at startup; non-support calls made
# before a restart are forgotten, the 90% default limits absorb that.
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


def pool_for(model: str | None, *, billed: bool = False) -> str:
    if billed:
        return BILLED
    name = (model or "").lower()
    return SMALL if "mini" in name or "nano" in name else BIG


def record(model: Any, tokens: Any, *, billed: bool = False) -> None:
    """Never raises: accounting must not break a reply."""
    try:
        amount = int(tokens)
    except (TypeError, ValueError):
        return
    if amount <= 0:
        return
    _roll()
    pool = pool_for(str(model or ""), billed=billed)
    _used[pool] = _used.get(pool, 0) + amount


def record_response(response: Any, *, billed: bool = False) -> None:
    usage = getattr(response, "usage", None)
    record(getattr(response, "model", None), getattr(usage, "total_tokens", None), billed=billed)


def used(pool: str) -> int:
    _roll()
    return _used.get(pool, 0)


def has_room(pool: str, settings: Any) -> bool:
    """An unset limit means unlimited; 0 disables the pool."""
    limit = getattr(settings, f"openai_daily_{pool}_token_limit", None)
    return limit is None or used(pool) < int(limit)


def is_paused(settings: Any) -> bool:
    return not has_room(SMALL, settings)


def pick_model(preferred: str, settings: Any) -> str | None:
    """Model for a tool-free call: None when the small pool is spent (AI paused),
    the mini model when a big model is asked for but the big pool is spent."""
    if is_paused(settings):
        return None
    if pool_for(preferred) == BIG and not has_room(BIG, settings):
        return getattr(settings, "openai_model", preferred)
    return preferred


def escalation_route(settings: Any) -> tuple[str, str, bool] | None:
    """(model, reasoning effort, use_tools) for a second opinion, or None when nothing is left.
    Billed big model with tools → free big model without tools → free mini at high effort."""
    big_model = getattr(settings, "openai_support_escalation_model", "gpt-5")
    effort = getattr(settings, "openai_support_reasoning_effort", "medium")
    if has_room(BILLED, settings):
        return big_model, effort, True
    if has_room(BIG, settings):
        return big_model, effort, False
    if has_room(SMALL, settings):
        return getattr(settings, "openai_model", "gpt-5-mini"), "high", False
    return None


def seed(rows: Any) -> None:
    """Rows of (model, billed, tokens) already spent today."""
    for model, billed, tokens in rows:
        record(model, tokens, billed=bool(billed))
