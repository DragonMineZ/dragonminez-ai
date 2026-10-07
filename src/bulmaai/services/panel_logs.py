"""Passive bot logs shown in the web panel (table panel_logs) instead of a Discord channel.

Everything here is best-effort: a logging failure must never break the feature that logged."""

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import discord

from bulmaai.database.db import get_pool

log = logging.getLogger(__name__)

RETENTION_DAYS = 365
PRUNE_EVERY_SECONDS = 24 * 3600
MAX_TITLE = 200
MAX_BODY = 4000
# Failures here must not be forwarded to Discord/panel again (that would loop).
_QUIET = {"suppress_discord_forward": True}

_last_prune = 0.0


@dataclass(frozen=True, slots=True)
class PanelLog:
    id: int
    level: int
    source: str
    title: str
    body: str
    user_id: int | None
    data: dict[str, Any]
    created_at: datetime


def embed_text(embed: discord.Embed) -> str:
    parts = [embed.description or ""]
    parts += [f"{field.name}: {field.value}" for field in embed.fields]
    return "\n".join(part for part in parts if part)


async def record(
    source: str,
    title: str,
    body: str = "",
    *,
    level: int = logging.INFO,
    user_id: int | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    try:
        pool = await get_pool()
        await pool.execute(
            "INSERT INTO panel_logs (level, source, title, body, user_id, data) VALUES ($1, $2, $3, $4, $5, $6::jsonb)",
            level,
            source[:32],
            title[:MAX_TITLE],
            body[:MAX_BODY],
            user_id,
            json.dumps(data or {}, default=str),
        )
        await _maybe_prune(pool)
    except Exception:
        log.warning("Couldn't write a panel log", exc_info=True, extra=_QUIET)


async def _maybe_prune(pool) -> None:
    # ponytail: lazy daily prune piggybacking on writes; a dedicated task if logging ever goes quiet for days.
    global _last_prune
    now = time.monotonic()
    if _last_prune and now - _last_prune < PRUNE_EVERY_SECONDS:
        return
    _last_prune = now
    await pool.execute(f"DELETE FROM panel_logs WHERE created_at < now() - interval '{RETENTION_DAYS} days'")


def _log(row) -> PanelLog:
    data = row["data"]
    return PanelLog(
        id=row["id"],
        level=row["level"],
        source=row["source"],
        title=row["title"],
        body=row["body"],
        user_id=row["user_id"],
        data=json.loads(data) if isinstance(data, str) else data,
        created_at=row["created_at"],
    )


async def list_logs(
    *,
    source: str | None = None,
    min_level: int | None = None,
    user_id: int | None = None,
    text: str | None = None,
    before_id: int | None = None,
    limit: int = 50,
) -> list[PanelLog]:
    conditions: list[str] = []
    args: list[Any] = []

    def add(condition: str, value: Any) -> None:
        args.append(value)
        conditions.append(condition.format(n=len(args)))

    if source:
        add("source = ${n}", source)
    if min_level is not None:
        add("level >= ${n}", min_level)
    if user_id is not None:
        add("user_id = ${n}", user_id)
    if text:
        add("(title ILIKE ${n} OR body ILIKE ${n})", f"%{text}%")
    if before_id is not None:
        add("id < ${n}", before_id)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    args.append(limit)
    pool = await get_pool()
    rows = await pool.fetch(f"SELECT * FROM panel_logs {where} ORDER BY id DESC LIMIT ${len(args)}", *args)
    return [_log(row) for row in rows]


async def sources() -> list[str]:
    pool = await get_pool()
    return [row["source"] for row in await pool.fetch("SELECT DISTINCT source FROM panel_logs ORDER BY 1")]
