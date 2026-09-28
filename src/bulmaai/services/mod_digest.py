"""Weekly moderation digest: aggregate queries over mod_cases/automod_hits/scam_image_hashes
(collect) and a pure embed builder (build_embeds) that turns that data into one or two embeds
for staff. Sent by cogs/mod_digest.py every Monday, and available on demand via /digest."""

from dataclasses import dataclass
from datetime import datetime, timedelta

import discord

from bulmaai.database.db import get_pool
from bulmaai.services import automod_hits, mod_cases

REPEAT_OFFENDER_MIN_CASES = 3
TOP_LIMIT = 5
FIELD_VALUE_LIMIT = 1024  # Discord's embed field value cap


@dataclass(frozen=True, slots=True)
class ActionCount:
    action: str
    this_week: int
    last_week: int


@dataclass(frozen=True, slots=True)
class ActorCount:
    human: bool  # True: moderator_id is set (a staff member acted); False: an automatic source
    this_week: int
    last_week: int


@dataclass(frozen=True, slots=True)
class ModeratorTotal:
    moderator_id: int
    count: int


@dataclass(frozen=True, slots=True)
class RepeatOffender:
    user_id: int
    count: int


@dataclass(frozen=True, slots=True)
class AppealStats:
    received: int
    accepted: int
    denied: int


@dataclass(frozen=True, slots=True)
class ScamImageStats:
    matches: int  # automod_hits with reason='scam_image' this week
    confirmed: int
    false_positives: int
    list_size: int  # total rows in scam_image_hashes


@dataclass(frozen=True, slots=True)
class DigestData:
    guild_id: int
    period_start: datetime
    period_end: datetime
    action_counts: tuple[ActionCount, ...]
    actor_counts: tuple[ActorCount, ...]
    top_moderators: tuple[ModeratorTotal, ...]
    repeat_offenders: tuple[RepeatOffender, ...]
    filter_stats: tuple[automod_hits.FilterStats, ...]
    unreviewed_automod_hits: int
    scam_images: ScamImageStats
    appeals: AppealStats
    reports_received: int
    antiraid_actions: int


# --- queries (one per concern) -------------------------------------------------------------------


async def _action_counts(pool, guild_id: int, week_start: datetime, prev_start: datetime) -> tuple[ActionCount, ...]:
    rows = await pool.fetch(
        """
        SELECT action,
               count(*) FILTER (WHERE created_at >= $2) AS this_week,
               count(*) FILTER (WHERE created_at >= $3 AND created_at < $2) AS last_week
        FROM mod_cases
        WHERE guild_id = $1 AND created_at >= $3
        GROUP BY action
        ORDER BY this_week DESC, last_week DESC
        """,
        guild_id,
        week_start,
        prev_start,
    )
    return tuple(ActionCount(row["action"], row["this_week"], row["last_week"]) for row in rows)


async def _actor_counts(pool, guild_id: int, week_start: datetime, prev_start: datetime) -> tuple[ActorCount, ...]:
    rows = await pool.fetch(
        """
        SELECT (moderator_id IS NOT NULL) AS human,
               count(*) FILTER (WHERE created_at >= $2) AS this_week,
               count(*) FILTER (WHERE created_at >= $3 AND created_at < $2) AS last_week
        FROM mod_cases
        WHERE guild_id = $1 AND created_at >= $3 AND action NOT IN ('report', 'appeal')
        GROUP BY human
        ORDER BY human DESC
        """,
        guild_id,
        week_start,
        prev_start,
    )
    return tuple(ActorCount(row["human"], row["this_week"], row["last_week"]) for row in rows)


async def _top_moderators(guild_id: int, week_start: datetime, limit: int = TOP_LIMIT) -> tuple[ModeratorTotal, ...]:
    totals: dict[int, int] = {}
    for moderator_id, _action, count in await mod_cases.moderator_stats(guild_id, week_start):
        totals[moderator_id] = totals.get(moderator_id, 0) + count
    ranked = sorted(totals.items(), key=lambda item: item[1], reverse=True)[:limit]
    return tuple(ModeratorTotal(moderator_id, count) for moderator_id, count in ranked)


async def _repeat_offenders(pool, guild_id: int, week_start: datetime, limit: int = TOP_LIMIT) -> tuple[RepeatOffender, ...]:
    rows = await pool.fetch(
        """
        SELECT user_id, count(*) AS n FROM mod_cases
        WHERE guild_id = $1 AND created_at >= $2
          AND action NOT IN ('report', 'appeal', 'note', 'unban', 'untimeout')  -- things done TO an offender
        GROUP BY user_id HAVING count(*) >= $3
        ORDER BY n DESC LIMIT $4
        """,
        guild_id,
        week_start,
        REPEAT_OFFENDER_MIN_CASES,
        limit,
    )
    return tuple(RepeatOffender(row["user_id"], row["n"]) for row in rows)


async def _unreviewed_automod_hits(pool, guild_id: int, week_start: datetime) -> int:
    return await pool.fetchval(
        "SELECT count(*) FROM automod_hits WHERE guild_id = $1 AND created_at >= $2 AND outcome IS NULL",
        guild_id,
        week_start,
    )


async def _scam_list_size(pool) -> int:
    return await pool.fetchval("SELECT count(*) FROM scam_image_hashes")


async def _appeals(pool, guild_id: int, week_start: datetime) -> AppealStats:
    row = await pool.fetchrow(
        """
        SELECT
            count(*) FILTER (WHERE action = 'appeal') AS received,
            count(*) FILTER (WHERE action = 'unban' AND source = 'appeal') AS accepted,
            count(*) FILTER (WHERE action = 'note' AND reason LIKE 'Appeal denied%') AS denied
        FROM mod_cases
        WHERE guild_id = $1 AND created_at >= $2
        """,
        guild_id,
        week_start,
    )
    return AppealStats(row["received"], row["accepted"], row["denied"])


async def _reports_and_antiraid(pool, guild_id: int, week_start: datetime) -> tuple[int, int]:
    """reports_received is 0 today: submitting a report doesn't record a mod_cases row yet
    (see cogs/mod_interactions.py _submit_report). The query is ready for when it does."""
    row = await pool.fetchrow(
        """
        SELECT
            count(*) FILTER (WHERE action = 'report') AS reports,
            count(*) FILTER (WHERE source = 'antiraid') AS antiraid
        FROM mod_cases
        WHERE guild_id = $1 AND created_at >= $2
        """,
        guild_id,
        week_start,
    )
    return row["reports"], row["antiraid"]


async def collect(guild_id: int, *, now: datetime) -> DigestData:
    """Aggregates the last 7 days (and the 7 before, for deltas) of moderation activity."""
    pool = await get_pool()
    week_start = now - timedelta(days=7)
    prev_start = now - timedelta(days=14)

    filter_stats = await automod_hits.filter_stats(guild_id, week_start)
    scam_stat = next((stat for stat in filter_stats if stat.reason == "scam_image"), None)
    reports_received, antiraid_actions = await _reports_and_antiraid(pool, guild_id, week_start)

    return DigestData(
        guild_id=guild_id,
        period_start=week_start,
        period_end=now,
        action_counts=await _action_counts(pool, guild_id, week_start, prev_start),
        actor_counts=await _actor_counts(pool, guild_id, week_start, prev_start),
        top_moderators=await _top_moderators(guild_id, week_start),
        repeat_offenders=await _repeat_offenders(pool, guild_id, week_start),
        filter_stats=filter_stats,
        unreviewed_automod_hits=await _unreviewed_automod_hits(pool, guild_id, week_start),
        scam_images=ScamImageStats(
            matches=scam_stat.hits if scam_stat else 0,
            confirmed=scam_stat.confirmed if scam_stat else 0,
            false_positives=scam_stat.false_positives if scam_stat else 0,
            list_size=await _scam_list_size(pool),
        ),
        appeals=await _appeals(pool, guild_id, week_start),
        reports_received=reports_received,
        antiraid_actions=antiraid_actions,
    )


# --- pure embed builder ---------------------------------------------------------------------------


def _delta(this_week: int, last_week: int) -> str:
    diff = this_week - last_week
    if diff == 0:
        return str(this_week)
    arrow = "▲" if diff > 0 else "▼"
    return f"{this_week} ({arrow}{abs(diff)})"


def _join_capped(lines: list[str], limit: int = FIELD_VALUE_LIMIT) -> str:
    """Joins lines with newlines, dropping trailing rows (with a "+N more" note) to stay under limit."""
    kept: list[str] = []
    total = 0
    for line in lines:
        addition = len(line) + (1 if kept else 0)
        if total + addition > limit:
            break
        kept.append(line)
        total += addition
    omitted = len(lines) - len(kept)
    if not omitted:
        return "\n".join(kept)
    suffix = f"… and {omitted} more"
    while kept:
        candidate = "\n".join(kept) + "\n" + suffix
        if len(candidate) <= limit:
            return candidate
        kept.pop()
        omitted += 1
        suffix = f"… and {omitted} more"
    return suffix[:limit]


def _suggestion_line(suggestion: automod_hits.Suggestion) -> str:
    rate = f"{suggestion.false_positives}/{suggestion.hits} false positives"
    if suggestion.setting is not None:
        return f"**{suggestion.reason}**: {suggestion.setting} {suggestion.current} → {suggestion.suggested} ({rate})"
    return f"**{suggestion.reason}**: {suggestion.note} ({rate})"


def build_embeds(data: DigestData, settings) -> list[discord.Embed]:
    """Pure: no I/O. Returns one embed, or two when there's automod activity to report."""
    period = f"{data.period_start:%b %d} – {data.period_end:%b %d, %Y}"

    overview_fields: list[tuple[str, str]] = []
    if data.action_counts:
        lines = [f"**{c.action}**: {_delta(c.this_week, c.last_week)}" for c in data.action_counts]
        overview_fields.append(("Case actions", _join_capped(lines)))

    by_human = {c.human: c for c in data.actor_counts}
    actor_lines = []
    if True in by_human:
        c = by_human[True]
        actor_lines.append(f"Human staff: {_delta(c.this_week, c.last_week)}")
    if False in by_human:
        c = by_human[False]
        actor_lines.append(f"Automatic: {_delta(c.this_week, c.last_week)}")
    if actor_lines:
        overview_fields.append(("Who acted", "\n".join(actor_lines)))

    if data.top_moderators:
        lines = [f"<@{m.moderator_id}>: {m.count}" for m in data.top_moderators]
        overview_fields.append(("Top moderators", _join_capped(lines)))

    if data.repeat_offenders:
        lines = [f"<@{o.user_id}>: {o.count}" for o in data.repeat_offenders]
        overview_fields.append((f"Repeat offenders ({REPEAT_OFFENDER_MIN_CASES}+ cases)", _join_capped(lines)))

    appeals = data.appeals
    if appeals.received or appeals.accepted or appeals.denied:
        overview_fields.append(
            ("Appeals", f"Received: {appeals.received}\nAccepted: {appeals.accepted}\nDenied: {appeals.denied}")
        )

    if data.reports_received:
        overview_fields.append(("Reports", str(data.reports_received)))

    if data.antiraid_actions:
        overview_fields.append(("Anti-raid actions", str(data.antiraid_actions)))

    overview = discord.Embed(
        title="Weekly Moderation Digest",
        description=period,
        color=discord.Color.blurple(),
        timestamp=data.period_end,
    )
    for name, value in overview_fields:
        overview.add_field(name=name, value=value, inline=False)
    if not overview_fields:
        overview.description = f"{period}\n\nNo moderation activity recorded this week."

    automod_fields: list[tuple[str, str]] = []
    filter_rows = [stat for stat in data.filter_stats if stat.reason != "scam_image"]
    if filter_rows:
        lines = [f"**{s.reason}**: {s.hits} hits ({s.confirmed} confirmed, {s.false_positives} false positive)" for s in filter_rows]
        automod_fields.append(("Automod hits by filter", _join_capped(lines)))

    if data.unreviewed_automod_hits:
        automod_fields.append(("Unreviewed automod hits", str(data.unreviewed_automod_hits)))

    scam = data.scam_images
    if scam.matches or scam.list_size:
        mode = "enforcing" if getattr(settings, "moderation_scam_images_enforce", False) else "shadow — review only"
        reviewed = scam.confirmed + scam.false_positives
        scam_lines = [
            f"Mode: {mode}",
            f"Matches this week: {scam.matches}",
            f"Reviewed: {reviewed}/{scam.matches} ({scam.confirmed} confirmed, {scam.false_positives} false positive)",
            f"Known scam images: {scam.list_size}",
        ]
        automod_fields.append(("Scam images", "\n".join(scam_lines)))

    suggestions = automod_hits.suggest(list(data.filter_stats), settings)
    if suggestions:
        automod_fields.append(("Tuning suggestions", _join_capped([_suggestion_line(s) for s in suggestions])))

    embeds = [overview]
    if automod_fields:
        automod = discord.Embed(title="Automod & Filters", color=discord.Color.dark_gold(), timestamp=data.period_end)
        for name, value in automod_fields:
            automod.add_field(name=name, value=value, inline=False)
        embeds.append(automod)
    return embeds
