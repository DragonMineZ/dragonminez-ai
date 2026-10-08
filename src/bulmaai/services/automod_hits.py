"""Automod incidents (table automod_hits) and the tuning loop built on staff feedback: a "False positive"
click undoes what automod did and counts against that filter; a punitive click confirms the hit (and
an explicit Delete & learn teaches scam images). Per-filter false-positive rates turn into threshold suggestions."""

from dataclasses import dataclass
from datetime import datetime

import discord

from bulmaai.database.db import get_pool
from bulmaai.services import mod_actions, mod_cases, scam_images

FALSE_POSITIVE = "false_positive"
CONFIRMED = "confirmed"
_UPDATABLE = {"action", "details", "alert_message_id", "warn_case_id", "timed_out", "image_hashes"}

# ponytail: suggest once a filter has 3+ false positives making up 20%+ of its hits; tune here if noisy.
MIN_FALSE_POSITIVES = 3
MIN_FALSE_POSITIVE_RATE = 0.2
# filter reason -> (setting, how to loosen it)
_KNOBS: dict[str, tuple[str, object]] = {
    "excessive_caps": ("moderation_caps_percent", lambda value: min(value + 10, 95)),
    "excessive_emoji": ("moderation_emoji_limit", lambda value: value + 5),
    "wall_of_text": ("moderation_newline_limit", lambda value: value + 10),
    "mass_mention": ("moderation_mass_mention_limit", lambda value: value + 2),
    "fast_messages": ("moderation_fast_message_count", lambda value: value + 2),
    "duplicate_spam": ("moderation_duplicate_count", lambda value: value + 1),
    "image burst": ("moderation_image_burst_count", lambda value: value + 1),
    "link burst": ("moderation_link_burst_count", lambda value: value + 2),
    "scam_image": ("moderation_scam_image_distance", lambda value: max(value - 2, 0)),
    "zalgo": ("moderation_zalgo_enabled", lambda value: False),
    "everyone_ping": ("moderation_block_everyone_ping", lambda value: False),
}
_NOTES = {
    "banned_word": "Review moderation_banned_words; a wildcard may be matching normal words.",
    "blocked_domain": "Review moderation_blocked_domains.",
    "phishdestroy_domain": "Use the Allowlist button on false positives to exempt a domain.",
    "suspicious_shortener": "Shortener hits only alert; allowlist a shortener you trust.",
    "discord_invite": "Consider moderation_block_discord_invites = false, or excluding that channel.",
}


@dataclass(frozen=True, slots=True)
class AutomodHit:
    id: int
    guild_id: int
    user_id: int
    reason: str
    action: str
    details: str | None
    domains: tuple[str, ...]
    image_hashes: tuple[int, ...]  # unsigned dHashes
    scam_hash_id: int | None
    warn_case_id: int | None
    timed_out: bool
    alert_message_id: int | None
    outcome: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class FilterStats:
    reason: str
    hits: int
    confirmed: int
    false_positives: int

    @property
    def false_positive_rate(self) -> float:
        return self.false_positives / self.hits if self.hits else 0.0


@dataclass(frozen=True, slots=True)
class Suggestion:
    reason: str
    false_positives: int
    hits: int
    setting: str | None = None
    current: object = None
    suggested: object = None
    note: str | None = None


def _hit(row) -> AutomodHit:
    return AutomodHit(
        id=row["id"],
        guild_id=row["guild_id"],
        user_id=row["user_id"],
        reason=row["reason"],
        action=row["action"],
        details=row["details"],
        domains=tuple(row["domains"] or ()),
        image_hashes=tuple(scam_images.from_db(value) for value in row["image_hashes"] or ()),
        scam_hash_id=row["scam_hash_id"],
        warn_case_id=row["warn_case_id"],
        timed_out=row["timed_out"],
        alert_message_id=row["alert_message_id"],
        outcome=row["outcome"],
        created_at=row["created_at"],
    )


async def record_hit(
    *,
    guild_id: int,
    user_id: int,
    reason: str,
    action: str,
    details: str | None = None,
    domains: tuple[str, ...] = (),
    image_hashes: tuple[int, ...] = (),
    scam_hash_id: int | None = None,
) -> int:
    pool = await get_pool()
    return await pool.fetchval(
        """
        INSERT INTO automod_hits (guild_id, user_id, reason, action, details, domains, image_hashes, scam_hash_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id
        """,
        guild_id,
        user_id,
        reason,
        action,
        details,
        list(domains),
        [scam_images.to_db(value) for value in image_hashes],
        scam_hash_id,
    )


async def update_hit(hit_id: int, **fields: object) -> None:
    """Fill in what's known later: alert_message_id, warn_case_id, timed_out, action, image_hashes."""
    if not fields:
        return
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise ValueError(f"Can't update {sorted(unknown)}")
    if "image_hashes" in fields:
        fields["image_hashes"] = [scam_images.to_db(value) for value in fields["image_hashes"]]
    columns = list(fields)
    assignments = ", ".join(f"{column} = ${index}" for index, column in enumerate(columns, start=2))
    pool = await get_pool()
    await pool.execute(f"UPDATE automod_hits SET {assignments} WHERE id = $1", hit_id, *fields.values())


async def get_hit(hit_id: int) -> AutomodHit | None:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM automod_hits WHERE id = $1", hit_id)
    return _hit(row) if row else None


async def hit_for_alert(alert_message_id: int) -> AutomodHit | None:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM automod_hits WHERE alert_message_id = $1", alert_message_id)
    return _hit(row) if row else None


async def set_outcome(hit_id: int, outcome: str, reviewer_id: int | None) -> bool:
    """First review wins, except a false positive may overturn an earlier confirmation."""
    pool = await get_pool()
    result = await pool.execute(
        """
        UPDATE automod_hits SET outcome = $2, reviewed_by = $3, reviewed_at = now()
        WHERE id = $1 AND (outcome IS NULL OR ($2 = 'false_positive' AND outcome <> 'false_positive'))
        """,
        hit_id,
        outcome,
        reviewer_id,
    )
    return result.endswith(" 1")


async def filter_stats(guild_id: int, since: datetime) -> list[FilterStats]:
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT reason, count(*) AS hits,
               count(*) FILTER (WHERE outcome = 'confirmed') AS confirmed,
               count(*) FILTER (WHERE outcome = 'false_positive') AS false_positives
        FROM automod_hits WHERE guild_id = $1 AND created_at >= $2
        GROUP BY reason ORDER BY hits DESC
        """,
        guild_id,
        since,
    )
    return [FilterStats(row["reason"], row["hits"], row["confirmed"], row["false_positives"]) for row in rows]


def suggest(stats: list[FilterStats], settings) -> list[Suggestion]:
    """Loosening suggestions for filters staff keep marking as false positives."""
    suggestions = []
    for stat in stats:
        if stat.false_positives < MIN_FALSE_POSITIVES or stat.false_positive_rate < MIN_FALSE_POSITIVE_RATE:
            continue
        knob = _KNOBS.get(stat.reason)
        if knob is not None:
            setting, loosen = knob
            current = getattr(settings, setting)
            suggested = loosen(current)
            if suggested == current:
                continue
            suggestions.append(Suggestion(stat.reason, stat.false_positives, stat.hits, setting, current, suggested))
        else:
            note = _NOTES.get(stat.reason, "Review this filter's settings.")
            suggestions.append(Suggestion(stat.reason, stat.false_positives, stat.hits, note=note))
    return suggestions



async def fresh_suggestions(guild_id: int, stats: list[FilterStats], since: datetime, settings) -> list[Suggestion]:
    """suggest(), but a knob changed in the panel during the window is judged only on hits since that
    change; otherwise applying a suggestion just brings the next, looser one back on the same old hits."""
    suggestions = suggest(stats, settings)
    knobs = sorted({item.setting for item in suggestions if item.setting})
    if not knobs:
        return suggestions
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT target, max(created_at) AS changed FROM panel_audit_log
        WHERE action IN ('settings.set', 'settings.reset') AND target = ANY($1::text[]) GROUP BY target
        """,
        knobs,
    )
    changed = {row["target"]: row["changed"] for row in rows if row["changed"] > since}
    result = []
    for item in suggestions:
        when = changed.get(item.setting)
        if when is None:
            result.append(item)
            continue
        recent = [stat for stat in await filter_stats(guild_id, when) if stat.reason == item.reason]
        result.extend(suggest(recent, settings))
    return result

# --- staff feedback --------------------------------------------------------------------------------


async def mark_false_positive(
    bot: discord.Bot, guild: discord.Guild, hit: AutomodHit, moderator: discord.Member
) -> list[str]:
    """Undo what automod did for this hit. Returns what was undone, for the reply and the alert."""
    if not await set_outcome(hit.id, FALSE_POSITIVE, moderator.id):
        return ["already marked as a false positive"]
    undone = []
    if hit.warn_case_id and await mod_actions.end_case(
        bot, guild.id, hit.warn_case_id, ended_by=moderator.id, note="false positive"
    ):
        undone.append(f"warn #{hit.warn_case_id} removed")
    if hit.timed_out:
        try:
            await mod_actions.perform(
                bot,
                guild,
                action="untimeout",
                target_id=hit.user_id,
                moderator=moderator,
                reason=f"Automod false positive (hit #{hit.id})",
                source="alert",
                notify=False,
            )
            undone.append("timeout removed")
        except mod_actions.ModActionError as error:
            undone.append(f"timeout not removed: {error}")
    if hit.scam_hash_id and await scam_images.remove(hit.scam_hash_id):
        undone.append(f"scam image #{hit.scam_hash_id} removed from the list")
    if unlearned := await scam_images.remove_by_note(auto_learn_note(hit.id)):
        undone.append(f"{unlearned} auto-learned image(s) unlearned")
    return undone


def auto_learn_note(hit_id: int) -> str:
    return f"automod hit #{hit_id} (auto)"


async def auto_learn(hit_id: int, image_hashes: tuple[int, ...], bot_id: int) -> int:
    """Confirmed image bursts teach their images straight away (no "Delete & learn" click). Hashes already
    listed keep their old note, so "False positive" only unlearns what this hit newly added."""
    for value in image_hashes:
        await scam_images.add(value, source="learned", added_by=bot_id, note=auto_learn_note(hit_id))
    return len(image_hashes)


async def mark_confirmed(hit: AutomodHit, moderator_id: int, *, learn: bool = False) -> int:
    """A punitive click on the alert. Only an explicit "Delete & learn" (learn=True) adds the hit's images
    to the scam list; returns how many were learned."""
    await set_outcome(hit.id, CONFIRMED, moderator_id)  # first review wins; learning is separate
    if not learn:
        return 0
    for value in hit.image_hashes:
        await scam_images.add(value, source="learned", added_by=moderator_id, note=f"automod hit #{hit.id}")
    return len(hit.image_hashes)
