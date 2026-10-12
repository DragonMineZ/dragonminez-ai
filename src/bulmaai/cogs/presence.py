"""Rotating custom status: a shuffled deck of silly lines, some filled in with live server numbers."""

import logging
import random
import string
from datetime import datetime, timezone

import discord
from discord.ext import tasks

from bulmaai.database.db import get_pool
from bulmaai.utils.lifecycle import ReloadableCog

log = logging.getLogger(__name__)

ROTATE_MINUTES = 2  # Discord rate-limits presence updates; every 2 min is far below it.
STATS_EVERY = 5  # rotations between stat refreshes (~10 min)
MAX_STATUS = 128

# {name} placeholders come from collect_stats(); a line whose number is missing or 0 is skipped.
LINES = (
    # live numbers
    "Babysitting {members} Saiyans",
    "{members} members and not one of them read the rules",
    "Counting {members} power levels. It's over 9000.",
    "{online} people online, {members} pretending they aren't",
    "Watching {online} of you type and delete",
    "{boosts} boosts. The Dragon Balls thank you.",
    "Answered {ai_today} questions today. Wish granted.",
    "{ai_today} answers today and zero thank-yous (jk ily)",
    "{ai_today} questions today. Half were 'where's the mod?'",
    "Ticket #{tickets_total} and counting",
    "Opened {tickets_total} tickets. Closed most of them.",
    "{tickets_open} tickets open. Staff, the Dragon Radar is beeping.",
    "Juggling {tickets_open} tickets like Senzu beans",
    "{bugs_week} bugs reported this week. Not features, apparently.",
    "Squashing {bugs_week} bugs. Frieza had fewer forms.",
    "Handed out {warns_week} warns this week. Be nice.",
    "{warns_week} warns this week. Vegeta would be proud.",
    "Deleted {automod_today} cursed messages today",
    "Blocked {automod_today} scams today. Not today, Frieza.",
    "Top power level here: {top_level}. Train harder.",
    "Someone hit level {top_level}. Touch grass maybe?",
    "{jar_downloads} dev jar downloads. Beta testers, assemble.",
    "Awake for {uptime}. Need a Senzu bean.",
    "Up {uptime} without a crash. Unlike your modpack.",
    "{bans_total} bans and counting. Don't be #{bans_next}.",
    # DragonMineZ / DBZ nonsense
    "Charging a Kamehameha… (ETA: 3 episodes)",
    "Reading your latest.log so you don't have to",
    "Your crash log says Optifine. It's always Optifine.",
    "Have you tried turning Forge off and on again?",
    "Looking for the 7 Dragon Balls (found 2 and a sock)",
    "Powering up. This could take a few episodes.",
    "Asking Shenron for more RAM",
    "Fusing with the support team. Fusion failed.",
    "Translating Vegeta's screams to English",
    "Arguing with Goku about who ate my lunch",
    "Training in the Hyperbolic Time Chamber",
    "Losing to Yamcha at Minecraft",
    "Teaching Piccolo to fly in creative mode",
    "Still waiting for Namek to explode (5 min, trust)",
    "Grinding TP so you don't have to",
    "Mining for Dragon Balls at Y=-58",
    "Explaining ki blasts to a creeper",
    "Pretending the Capsule Corp logo is mine",
    "Building a Saiyan pod out of cobblestone",
    "Doing Bulma things. Very genius. Very busy.",
    "Fixing the Dragon Radar with duct tape",
    "Calculating your power level… scouter broke",
    "Hosting a tournament nobody signed up for",
    "Mod update? Soon™",
    "Patching bugs faster than Goku eats",
    "Reading every bug report. Even 'it no work'.",
    "Allocating 2GB of RAM to Shenron's ego",
    "Not an AI, just a very fast Bulma",
    "Holding the server together with Senzu beans",
    "Rewriting history so Yamcha wins once",
    "Wishing for no more 'mod doesn't load' tickets",
    "Napping between tickets (don't tell staff)",
    "Running on 3 coffees and a Capsule",
    "Ignoring Master Roshi's DMs",
    "Spamming Instant Transmission between channels",
    "Making sure nobody pings everyone. Ever.",
    "Waiting for you to send your latest.log",
    "Brb, refusing to explain fabric vs forge again",
    "Speedrunning the support queue",
    "Dodging Kid Buu's chocolate beam",
    "Turning bug reports into Super Saiyan bug reports",
)

_FIELDS = string.Formatter()


def needed(line: str) -> set[str]:
    return {field for _, field, _, _ in _FIELDS.parse(line) if field}


def render(line: str, stats: dict) -> str | None:
    """The line with numbers filled in, or None if it needs a stat we don't have (or that's zero)."""
    if any(not stats.get(field) for field in needed(line)):
        return None
    shown = {key: f"{value:,}" if isinstance(value, int) else value for key, value in stats.items()}
    return line.format(**shown)[:MAX_STATUS]


def uptime_text(since: datetime, now: datetime) -> str | None:
    minutes = int((now - since).total_seconds() // 60)
    days, hours = divmod(minutes // 60, 24)
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {minutes % 60}m" if minutes >= 1 else None


class Deck:
    """Every line once in random order, then reshuffle; lines that can't render right now are skipped."""

    def __init__(self, lines=LINES, rng: random.Random | None = None) -> None:
        self.lines, self.rng, self.queue = list(lines), rng or random.Random(), []

    def next(self, stats: dict) -> str:
        for _ in range(2 * len(self.lines)):
            if not self.queue:
                self.queue = self.rng.sample(self.lines, len(self.lines))
            if text := render(self.queue.pop(), stats):
                return text
        return "Doing Bulma things"


async def db_stats() -> dict:
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        SELECT
          (SELECT count(*) FROM support_ai_traces WHERE created_at >= date_trunc('day', now())) AS ai_today,
          (SELECT last_number FROM ticket_counter WHERE id = 1) AS tickets_total,
          (SELECT count(*) FROM tickets WHERE status = 'open') AS tickets_open,
          (SELECT count(*) FROM bug_reports WHERE created_at > now() - interval '7 days') AS bugs_week,
          (SELECT count(*) FROM mod_cases WHERE action = 'warn' AND created_at > now() - interval '7 days') AS warns_week,
          (SELECT count(*) FROM mod_cases WHERE action = 'ban') AS bans_total,
          (SELECT count(*) FROM automod_hits WHERE created_at >= date_trunc('day', now())) AS automod_today,
          (SELECT max(level) FROM member_activity) AS top_level,
          (SELECT count(*) FROM dev_jar_user_downloads) AS jar_downloads
        """
    )
    stats = {key: int(value) for key, value in dict(row).items() if value is not None}
    if stats.get("bans_total"):
        stats["bans_next"] = stats["bans_total"] + 1
    return stats


class PresenceCog(ReloadableCog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.deck = Deck()
        self.stats: dict = {}
        self.started = datetime.now(timezone.utc)

    async def on_startup(self) -> None:
        if not self._rotate.is_running():
            self._rotate.start()

    async def on_shutdown(self) -> None:
        self._rotate.cancel()

    async def _refresh(self) -> None:
        stats: dict = {}
        guild = self.bot.get_guild(self.bot.settings.panel_guild_id)
        if guild is not None:
            stats |= {"members": guild.member_count or 0, "boosts": guild.premium_subscription_count or 0}
            try:
                counted = await self.bot.fetch_guild(guild.id, with_counts=True)
                stats["online"] = counted.approximate_presence_count or 0
            except discord.HTTPException:
                pass
        try:
            stats |= await db_stats()
        except Exception:
            log.warning("Presence stats query failed", exc_info=True)
        self.stats = stats

    @tasks.loop(minutes=ROTATE_MINUTES)
    async def _rotate(self) -> None:
        try:
            if self._rotate.current_loop % STATS_EVERY == 0:
                await self._refresh()
            stats = {**self.stats, "uptime": uptime_text(self.started, datetime.now(timezone.utc))}
            await self.bot.change_presence(activity=discord.CustomActivity(name=self.deck.next(stats)))
        except Exception:
            log.warning("Presence rotation failed", exc_info=True)

    @_rotate.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()


def setup(bot: discord.Bot):
    bot.add_cog(PresenceCog(bot))
