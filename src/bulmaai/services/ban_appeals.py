"""Ban/timeout appeals sent through the appeals website (table ban_appeals). One open appeal per user (outcome NULL,
enforced by a partial unique index); staff Accept/Deny claim the row WHERE outcome IS NULL, so the first click wins.
The staff card in appeals_channel_id is rebuilt from the row on every change (ui/appeal_cards.py)."""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import discord

from bulmaai.database.db import get_pool
from bulmaai.services import mod_cases
from bulmaai.ui import appeal_cards

log = logging.getLogger(__name__)

NO_MENTIONS = discord.AllowedMentions.none()
WHY_MAX = 1500
WHY_MIN = 20
EXTRA_MAX = 500
QUESTION_MAX = 500
REPLY_MAX = 1000


@dataclass(frozen=True)
class Appeal:
    id: int
    user_id: int
    user_name: str
    kind: str  # ban | timeout
    ban_reason: str | None
    punished_at: datetime | None
    ends_at: datetime | None
    answers: dict[str, str] = field(default_factory=dict)
    created_at: datetime | None = None
    outcome: str | None = None  # None (open) | accepted | denied
    outcome_by: int | None = None
    outcome_at: datetime | None = None
    staff_note: str | None = None
    question: str | None = None
    question_by: int | None = None
    question_asked_at: datetime | None = None
    user_reply: str | None = None
    reply_at: datetime | None = None
    card_channel_id: int | None = None
    card_message_id: int | None = None


def _appeal(row: Any) -> Appeal | None:
    if row is None:
        return None
    answers = row["answers"]
    if isinstance(answers, str):
        answers = json.loads(answers or "{}")
    return Appeal(
        id=row["id"],
        user_id=row["user_id"],
        user_name=row["user_name"] or "",
        kind=row["kind"],
        ban_reason=row["ban_reason"],
        punished_at=row["punished_at"],
        ends_at=row["ends_at"],
        answers={str(k): str(v) for k, v in (answers or {}).items()},
        created_at=row["created_at"],
        outcome=row["outcome"],
        outcome_by=row["outcome_by"],
        outcome_at=row["outcome_at"],
        staff_note=row["staff_note"],
        question=row["question"],
        question_by=row["question_by"],
        question_asked_at=row["question_asked_at"],
        user_reply=row["user_reply"],
        reply_at=row["reply_at"],
        card_channel_id=row["card_channel_id"],
        card_message_id=row["card_message_id"],
    )


# --- settings --------------------------------------------------------------------------------------------------------


def site_url(settings) -> str | None:
    """The appeals page address, or None while the website is off (no staff channel or no public URL)."""
    base = (getattr(settings, "appeals_public_url", "") or "").strip().rstrip("/")
    if not base or not getattr(settings, "appeals_channel_id", None):
        return None
    return base + "/appeal/"


def login_ready(settings) -> bool:
    """Discord login needs the bot app's OAuth client and a secret to sign the session cookie."""
    return bool(
        getattr(settings, "discord_oauth_client_id", None)
        and getattr(settings, "discord_oauth_client_secret", None)
        and getattr(settings, "panel_session_secret", None)
    )


def next_allowed(appeal: Appeal | None, cooldown_days: int) -> datetime | None:
    """When someone whose latest appeal was denied may appeal again; None when nothing holds them back."""
    if appeal is None or appeal.outcome != "denied" or appeal.outcome_at is None or cooldown_days <= 0:
        return None
    at = appeal.outcome_at + timedelta(days=cooldown_days)
    return at if at > datetime.now(timezone.utc) else None


# --- storage ---------------------------------------------------------------------------------------------------------


async def latest(user_id: int) -> Appeal | None:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM ban_appeals WHERE user_id = $1 ORDER BY created_at DESC, id DESC LIMIT 1", user_id
    )
    return _appeal(row)


async def get(appeal_id: int) -> Appeal | None:
    pool = await get_pool()
    return _appeal(await pool.fetchrow("SELECT * FROM ban_appeals WHERE id = $1", appeal_id))


async def create(
    *,
    user_id: int,
    user_name: str,
    kind: str,
    ban_reason: str | None,
    punished_at: datetime | None,
    ends_at: datetime | None,
    answers: dict[str, str],
) -> Appeal | None:
    """None when the user already has an open appeal."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO ban_appeals (user_id, user_name, kind, ban_reason, punished_at, ends_at, answers)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
        ON CONFLICT (user_id) WHERE outcome IS NULL DO NOTHING
        RETURNING *
        """,
        user_id,
        user_name[:100],
        kind,
        ban_reason,
        punished_at,
        ends_at,
        json.dumps(answers),
    )
    return _appeal(row)


async def set_card(appeal_id: int, channel_id: int, message_id: int) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE ban_appeals SET card_channel_id = $2, card_message_id = $3 WHERE id = $1",
        appeal_id,
        channel_id,
        message_id,
    )


async def claim(appeal_id: int, outcome: str, staff_id: int, note: str | None = None) -> Appeal | None:
    """First click wins: None when someone already decided this appeal."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        UPDATE ban_appeals SET outcome = $2, outcome_by = $3, outcome_at = now(), staff_note = $4
        WHERE id = $1 AND outcome IS NULL
        RETURNING *
        """,
        appeal_id,
        outcome,
        staff_id,
        note or None,
    )
    return _appeal(row)


async def release(appeal_id: int, staff_id: int) -> None:
    """Gives back a claim whose action failed (only the clicker's own)."""
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE ban_appeals SET outcome = NULL, outcome_by = NULL, outcome_at = NULL, staff_note = NULL
        WHERE id = $1 AND outcome_by = $2 AND outcome IS NOT NULL
        """,
        appeal_id,
        staff_id,
    )


async def ask(appeal_id: int, question: str, staff_id: int) -> Appeal | None:
    """A new question replaces the last one (and its answer); None once the appeal is decided."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        UPDATE ban_appeals SET question = $2, question_by = $3, question_asked_at = now(), user_reply = NULL,
            reply_at = NULL
        WHERE id = $1 AND outcome IS NULL
        RETURNING *
        """,
        appeal_id,
        question,
        staff_id,
    )
    return _appeal(row)


async def reply(appeal_id: int, user_id: int, text: str) -> Appeal | None:
    """The user's answer to the open question; None if there's no unanswered question on their open appeal."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        UPDATE ban_appeals SET user_reply = $3, reply_at = now()
        WHERE id = $1 AND user_id = $2 AND outcome IS NULL AND question IS NOT NULL AND user_reply IS NULL
        RETURNING *
        """,
        appeal_id,
        user_id,
        text,
    )
    return _appeal(row)


# --- the staff card ----------------------------------------------------------------------------------------------------


async def history(guild_id: int, user_id: int) -> list[mod_cases.ModCase]:
    try:
        return await mod_cases.list_cases(guild_id, user_id=user_id, limit=25)
    except Exception:
        log.warning("Couldn't load cases for an appeal card", exc_info=True, extra={"user_id": user_id})
        return []


async def render(bot: discord.Bot, appeal: Appeal) -> discord.ui.DesignerView:
    cases = await history(bot.settings.panel_guild_id, appeal.user_id)
    return appeal_cards.appeal_card(appeal, cases)


async def _channel(bot: discord.Bot, channel_id: int | None):
    if channel_id is None:
        return None
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            log.warning("Can't reach the appeals channel %s", channel_id, exc_info=True)
            return None
    return channel if hasattr(channel, "send") else None


async def post_card(bot: discord.Bot, appeal: Appeal) -> bool:
    """Posts the staff card for a new appeal and remembers it on the row."""
    channel = await _channel(bot, bot.settings.appeals_channel_id)
    if channel is None:
        log.error("Couldn't post appeal #%s: no appeals channel", appeal.id, extra={"event": "ban_appeal_post_failed"})
        return False
    try:
        message = await channel.send(view=await render(bot, appeal), allowed_mentions=NO_MENTIONS)
        await set_card(appeal.id, channel.id, message.id)
    except Exception:
        log.exception("Couldn't post appeal #%s", appeal.id, extra={"event": "ban_appeal_post_failed"})
        return False
    return True


async def card_message(bot: discord.Bot, appeal: Appeal) -> discord.Message | None:
    channel = await _channel(bot, appeal.card_channel_id)
    if channel is None or appeal.card_message_id is None:
        return None
    try:
        return await channel.fetch_message(appeal.card_message_id)
    except discord.HTTPException:
        log.warning("Couldn't fetch the card of appeal #%s", appeal.id, exc_info=True)
        return None


async def note_reply(bot: discord.Bot, appeal: Appeal) -> None:
    """After the user answered on the website: the card shows the answer and a reply under it keeps it in the channel
    (a later question replaces it on the card)."""
    message = await card_message(bot, appeal)
    if message is None:
        return
    try:
        await message.edit(view=await render(bot, appeal), allowed_mentions=NO_MENTIONS)
        await message.reply(view=appeal_cards.reply_card(appeal), allowed_mentions=NO_MENTIONS, mention_author=False)
    except discord.HTTPException:
        log.warning("Couldn't update the card of appeal #%s", appeal.id, exc_info=True)
