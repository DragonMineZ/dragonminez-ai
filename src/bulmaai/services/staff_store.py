"""Staff Bulma's data (scripts/schema.sql, staff_* tables): profiles, the message vault, memory notes, reminders,
pending actions waiting for a confirm click, and MCP tokens. forget() removes all of a staff member's rows."""

import hashlib
import json
import secrets
from datetime import datetime
from typing import Any

from bulmaai.database.db import get_pool

CONTENT_MAX = 4000
NOTE_MAX = 300
REMINDER_MAX = 500
NOTE_SOURCES = ("distill", "user")
ORIGINS = ("discord", "mcp")
OUTCOMES = ("running", "done", "failed", "cancelled", "expired")

_ACTION_COLUMNS = (
    "id, user_id, guild_id, action, args, summary, origin, channel_id, message_id, created_at, expires_at, "
    "outcome, outcome_at, result"
)


def _cap(text: str, limit: int) -> str:
    return (text or "").strip()[:limit]


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _action(row: Any) -> dict | None:
    if row is None:
        return None
    data = dict(row)
    args = data.get("args")
    data["args"] = json.loads(args) if isinstance(args, str) else dict(args or {})
    return data


# Profiles


async def get_profile(user_id: int) -> dict | None:
    row = await (await get_pool()).fetchrow("SELECT * FROM staff_profiles WHERE user_id = $1", user_id)
    return dict(row) if row else None


async def save_profile(user_id: int, *, nickname: str, language: str | None, timezone: str, ticket_info: bool) -> None:
    await (await get_pool()).execute(
        """
        INSERT INTO staff_profiles (user_id, nickname, language, timezone, ticket_info, consent_at)
        VALUES ($1, $2, $3, $4, $5, now())
        ON CONFLICT (user_id) DO UPDATE SET nickname = $2, language = $3, timezone = $4, ticket_info = $5,
            updated_at = now()
        """,
        user_id,
        _cap(nickname, 64),
        language,
        timezone or "UTC",
        ticket_info,
    )


async def list_profiles() -> list[dict]:
    rows = await (await get_pool()).fetch("SELECT * FROM staff_profiles ORDER BY user_id")
    return [dict(row) for row in rows]


async def mark_learned(user_id: int) -> None:
    await (await get_pool()).execute("UPDATE staff_profiles SET learned_at = now() WHERE user_id = $1", user_id)


async def mark_distilled(user_id: int) -> None:
    await (await get_pool()).execute("UPDATE staff_profiles SET distilled_at = now() WHERE user_id = $1", user_id)


async def forget(user_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM staff_profiles WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM staff_vault WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM staff_memory WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM staff_reminders WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM staff_actions WHERE user_id = $1 AND outcome IS NULL", user_id)
            await conn.execute("DELETE FROM staff_mcp_tokens WHERE user_id = $1", user_id)


# Vault


async def store_message(
    message_id: int, user_id: int, guild_id: int, channel_id: int, content: str, created_at: datetime
) -> None:
    content = _cap(content, CONTENT_MAX)
    if not content:
        return
    await (await get_pool()).execute(
        """
        INSERT INTO staff_vault (message_id, user_id, guild_id, channel_id, content, created_at)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (message_id) DO NOTHING
        """,
        message_id,
        user_id,
        guild_id,
        channel_id,
        content,
        created_at,
    )


async def edit_message(message_id: int, content: str, edited_at: datetime) -> None:
    await (await get_pool()).execute(
        "UPDATE staff_vault SET content = $2, edited_at = $3 WHERE message_id = $1",
        message_id,
        _cap(content, CONTENT_MAX),
        edited_at,
    )


async def delete_message(message_id: int) -> None:
    await (await get_pool()).execute("DELETE FROM staff_vault WHERE message_id = $1", message_id)


async def search_vault(user_id: int, query: str, limit: int = 10) -> list[dict]:
    rows = await (await get_pool()).fetch(
        """
        SELECT content, channel_id, created_at FROM staff_vault
        WHERE user_id = $1 AND tsv @@ websearch_to_tsquery('simple', $2)
        ORDER BY created_at DESC LIMIT $3
        """,
        user_id,
        query,
        limit,
    )
    return [dict(row) for row in rows]


async def recent_vault(user_id: int, limit: int) -> list[dict]:
    rows = await (await get_pool()).fetch(
        "SELECT content, channel_id, created_at FROM staff_vault WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2",
        user_id,
        limit,
    )
    return [dict(row) for row in rows]


async def vault_since(user_id: int, since: datetime, limit: int) -> list[dict]:
    rows = await (await get_pool()).fetch(
        """
        SELECT content, channel_id, created_at FROM staff_vault
        WHERE user_id = $1 AND created_at > $2 ORDER BY created_at DESC LIMIT $3
        """,
        user_id,
        since,
        limit,
    )
    return [dict(row) for row in rows]


async def count_since(user_id: int, since: datetime) -> int:
    count = await (await get_pool()).fetchval(
        "SELECT count(*) FROM staff_vault WHERE user_id = $1 AND created_at > $2", user_id, since
    )
    return int(count or 0)


async def export_vault(user_id: int) -> list[dict]:
    rows = await (await get_pool()).fetch(
        """
        SELECT message_id, guild_id, channel_id, content, created_at, edited_at FROM staff_vault
        WHERE user_id = $1 ORDER BY created_at
        """,
        user_id,
    )
    return [dict(row) for row in rows]


# Memory


async def list_notes(user_id: int) -> list[dict]:
    rows = await (await get_pool()).fetch(
        "SELECT id, note, source, created_at FROM staff_memory WHERE user_id = $1 ORDER BY id", user_id
    )
    return [dict(row) for row in rows]


async def add_note(user_id: int, note: str, source: str) -> int | None:
    if source not in NOTE_SOURCES:
        raise ValueError("Unknown note source.")
    note = _cap(note, NOTE_MAX)
    if not note:
        return None
    return await (await get_pool()).fetchval(
        "INSERT INTO staff_memory (user_id, note, source) VALUES ($1, $2, $3) RETURNING id", user_id, note, source
    )


async def replace_distilled(user_id: int, notes: list[str]) -> None:
    notes = [note for note in (_cap(note, NOTE_MAX) for note in notes) if note]
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM staff_memory WHERE user_id = $1 AND source = 'distill'", user_id)
            if notes:
                await conn.executemany(
                    "INSERT INTO staff_memory (user_id, note, source) VALUES ($1, $2, 'distill')",
                    [(user_id, note) for note in notes],
                )


async def delete_note(user_id: int, note_id: int) -> bool:
    status = await (await get_pool()).execute(
        "DELETE FROM staff_memory WHERE id = $1 AND user_id = $2", note_id, user_id
    )
    return status.endswith(" 1")


# Reminders


async def add_reminder(user_id: int, text: str, due_at: datetime) -> int:
    return await (await get_pool()).fetchval(
        "INSERT INTO staff_reminders (user_id, text, due_at) VALUES ($1, $2, $3) RETURNING id",
        user_id,
        _cap(text, REMINDER_MAX),
        due_at,
    )


async def due_reminders(now: datetime, limit: int = 50) -> list[dict]:
    rows = await (await get_pool()).fetch(
        """
        SELECT id, user_id, text, due_at, created_at FROM staff_reminders
        WHERE sent_at IS NULL AND due_at <= $1 ORDER BY due_at LIMIT $2
        """,
        now,
        limit,
    )
    return [dict(row) for row in rows]


async def claim_reminder(reminder_id: int) -> bool:
    status = await (await get_pool()).execute(
        "UPDATE staff_reminders SET sent_at = now() WHERE id = $1 AND sent_at IS NULL", reminder_id
    )
    return status.endswith(" 1")


async def list_reminders(user_id: int) -> list[dict]:
    rows = await (await get_pool()).fetch(
        "SELECT id, text, due_at, created_at FROM staff_reminders WHERE user_id = $1 AND sent_at IS NULL ORDER BY due_at",
        user_id,
    )
    return [dict(row) for row in rows]


# Pending actions


async def create_action(
    user_id: int, guild_id: int, action: str, args: dict, summary: str, origin: str, ttl_seconds: int = 900
) -> int:
    if origin not in ORIGINS:
        raise ValueError("Unknown origin.")
    return await (await get_pool()).fetchval(
        """
        INSERT INTO staff_actions (user_id, guild_id, action, args, summary, origin, expires_at)
        VALUES ($1, $2, $3, $4::jsonb, $5, $6, now() + make_interval(secs => $7))
        RETURNING id
        """,
        user_id,
        guild_id,
        action,
        json.dumps(args, ensure_ascii=False),
        _cap(summary, 1000),
        origin,
        float(ttl_seconds),
    )


async def set_action_card(action_id: int, channel_id: int, message_id: int) -> None:
    await (await get_pool()).execute(
        "UPDATE staff_actions SET channel_id = $2, message_id = $3 WHERE id = $1", action_id, channel_id, message_id
    )


async def get_action(action_id: int) -> dict | None:
    row = await (await get_pool()).fetchrow(f"SELECT {_ACTION_COLUMNS} FROM staff_actions WHERE id = $1", action_id)
    return _action(row)


async def claim_action(action_id: int, user_id: int) -> dict | None:
    row = await (await get_pool()).fetchrow(
        f"""
        UPDATE staff_actions SET outcome = 'running', outcome_at = now()
        WHERE id = $1 AND user_id = $2 AND outcome IS NULL AND expires_at > now()
        RETURNING {_ACTION_COLUMNS}
        """,
        action_id,
        user_id,
    )
    return _action(row)


async def finish_action(action_id: int, outcome: str, result: str | None) -> None:
    if outcome not in OUTCOMES:
        raise ValueError("Unknown outcome.")
    await (await get_pool()).execute(
        "UPDATE staff_actions SET outcome = $2, outcome_at = now(), result = $3 WHERE id = $1",
        action_id,
        outcome,
        _cap(result, 2000) if result is not None else None,
    )


async def release_action(action_id: int) -> bool:
    status = await (await get_pool()).execute(
        "UPDATE staff_actions SET outcome = NULL, outcome_at = NULL WHERE id = $1 AND outcome = 'running'", action_id
    )
    return status.endswith(" 1")


async def cancel_action(action_id: int, user_id: int) -> bool:
    status = await (await get_pool()).execute(
        """
        UPDATE staff_actions SET outcome = 'cancelled', outcome_at = now()
        WHERE id = $1 AND user_id = $2 AND outcome IS NULL
        """,
        action_id,
        user_id,
    )
    return status.endswith(" 1")


async def expire_actions(now: datetime) -> list[dict]:
    rows = await (await get_pool()).fetch(
        f"""
        UPDATE staff_actions SET outcome = 'expired', outcome_at = now()
        WHERE outcome IS NULL AND expires_at <= $1
        RETURNING {_ACTION_COLUMNS}
        """,
        now,
    )
    return [_action(row) for row in rows]


# MCP tokens


async def issue_token(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    await (await get_pool()).execute(
        """
        INSERT INTO staff_mcp_tokens (user_id, token_hash) VALUES ($1, $2)
        ON CONFLICT (user_id) DO UPDATE SET token_hash = $2, created_at = now(), last_used_at = NULL
        """,
        user_id,
        hash_token(token),
    )
    return token


async def user_for_token(token: str) -> int | None:
    if not token:
        return None
    return await (await get_pool()).fetchval(
        "UPDATE staff_mcp_tokens SET last_used_at = now() WHERE token_hash = $1 RETURNING user_id", hash_token(token)
    )


async def revoke_token(user_id: int) -> None:
    await (await get_pool()).execute("DELETE FROM staff_mcp_tokens WHERE user_id = $1", user_id)


# Request log (daily cap)


async def count_requests_since(user_id: int, since: datetime) -> int:
    count = await (await get_pool()).fetchval(
        "SELECT count(*) FROM staff_requests WHERE user_id = $1 AND created_at > $2", user_id, since
    )
    return int(count or 0)


async def log_request(user_id: int) -> None:
    pool = await get_pool()
    await pool.execute("DELETE FROM staff_requests WHERE created_at < now() - interval '2 days'")
    await pool.execute("INSERT INTO staff_requests (user_id) VALUES ($1)", user_id)
