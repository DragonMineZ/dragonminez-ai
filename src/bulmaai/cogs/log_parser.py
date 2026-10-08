import asyncio
import logging
import re
import time
from collections import defaultdict

import discord
from discord.ext import commands

from bulmaai.services.ai_guard import defang
from bulmaai.services.moderation import ModerationState
from bulmaai.utils.dmz_addons import check_addons
from bulmaai.utils.dmzdebug_parser import looks_like_dmzdebug
from bulmaai.utils.log_parser import parse_log, LogReport
from bulmaai.utils.permissions import is_admin
from bulmaai.ui.v2 import TEXT_LIMIT, card, fit

log = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = (".log", ".txt")
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
# ponytail: in-memory per-user window, a restart resets it.
AUTO_PARSE_MAX_PER_WINDOW = 3
AUTO_PARSE_WINDOW_SECONDS = 60

# High-confidence filenames that are always auto-parsed without admin approval.
_AUTO_PARSE_NAMES = {"latest.log", "debug.log", "crash-report.txt"}

# Reaction emoji used for admin approval of uncertain log files.
_APPROVE_EMOJI = "🔍"

# Strips "[24Jun2023 06:57:42.886] [Render thread/FATAL] [net.minecraftforge.ForgeMod/]:"
# from the front of raw log lines so only the human-readable message is shown.
_RE_STRIP_LOG_PREFIX = re.compile(
    r"^\[[^]]+]\s*\[[^]]+]\s*(?:\[[^]]*]:\s*)?"
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _clean_error_line(line: str) -> str:
    """Strip the [timestamp] [thread/LEVEL] [logger/]: prefix from a log line."""
    return _RE_STRIP_LOG_PREFIX.sub("", line).strip()


def _summarise_stacktrace(raw: str) -> str:
    """Return only the exception class/message lines and 'Caused by:' lines.

    Drops all 'at com.mojang...' lines so the card stays readable.
    Keeps at most 10 meaningful lines.
    """
    keep: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Skip frame lines — they're noise in a short card snippet
        if stripped.startswith("at ") or stripped.startswith("..."):
            continue
        keep.append(stripped)
        if len(keep) >= 10:
            break
    return "\n".join(keep)


def _is_high_confidence_name(filename: str) -> bool:
    """Return True if the filename is a well-known Minecraft log name."""
    lower = filename.lower()
    if lower in _AUTO_PARSE_NAMES:
        return True
    # Forge crash reports: crash-2024-01-01_12.00.00-server.txt etc.
    if lower.startswith("crash-") and lower.endswith(".txt"):
        return True
    return False


# ── Card builder ──────────────────────────────────────────────────────────────

def _build_card(report: LogReport, filename: str) -> discord.ui.DesignerView:
    """The analysis as a V2 card: what went wrong first, then the environment, then the mod lists.
    Everything from the uploaded file is defanged (no live links or pings)."""
    has_errors = bool(report.errors)
    is_valid = report.is_forge or bool(report.mc_version)
    # Colour: red = errors present, orange = no Forge detected, green = clean
    if has_errors:
        colour = discord.Colour.red()
    elif not is_valid:
        colour = discord.Colour.orange()
    else:
        colour = discord.Colour.green()

    title = "## 🔍 Log analysis"
    if not is_valid:
        title += "\n⚠️ This file does not appear to be a Minecraft Forge log. Results may be incomplete."

    # ── What went wrong ───────────────────────────────────────────────────────
    if report.errors:
        cleaned = [_clean_error_line(e) for e in report.errors[:8]]
        problems = f"**❌ Errors / Fatal ({len(report.errors)})**\n" + "\n".join(f"• {_truncate(e, 120)}" for e in cleaned)
    else:
        problems = "**✅ Status**\nNo errors or fatal messages found."
    if report.stacktrace and (summary := _summarise_stacktrace(report.stacktrace)):
        problems += f"\n**📋 Exception Summary**\n```\n{_truncate(summary, 800)}\n```"

    # ── Environment ───────────────────────────────────────────────────────────
    env_lines = [
        f"{icon} **{name}** {value}"
        for icon, name, value in (
            ("🎮", "Minecraft", report.mc_version and f"`{report.mc_version}`"),
            ("⚙️", "Forge", report.forge_version and f"`{report.forge_version}`"),
            ("☕", "Java", report.java_version and f"`{report.java_version}`"),
            ("🐉", "DragonMineZ", report.dragonminez_version and f"`{report.dragonminez_version}`"),
            ("💻", "OS", report.operating_system),
            ("🧠", "Memory", report.memory),
        )
        if value
    ]
    environment = ("**🖥️ Environment**\n" + "\n".join(env_lines)) if env_lines else ""

    # ── Mods + DMZ addons ─────────────────────────────────────────────────────
    mod_count = len(report.mods)
    if mod_count:
        sorted_mods = sorted(report.mods.items())
        shown = sorted_mods if mod_count <= 20 else sorted_mods[:15]
        mods = f"**🧩 Mods Detected ({mod_count})**\n" + "\n".join(f"`{mid}` — {ver}" for mid, ver in shown)
        if mod_count > 20:
            mods += f"\n*…and **{mod_count - 15}** more*"
        if not report.dragonminez_version:
            mods += "\n-# ℹ️ DragonMineZ was not detected among the loaded mods."
    else:
        mods = "**🧩 Mods Detected**\n*No mods detected. Log may be incomplete or vanilla.*"
    addons = check_addons(report.mods, report.dragonminez_version)
    if addons:
        mods += f"\n**🐉 DMZ Addons ({len(addons)})**\n" + "\n".join(
            f"⚠️ `{a.mod_id}` — {a.version} *(needs DMZ ≥ {a.min_dmz_version})*"
            if a.compatible is False
            else f"✅ `{a.mod_id}` — {a.version}"
            for a in addons
        )

    footer = f"-# 📄 {discord.utils.escape_markdown(_truncate(filename, 100))}"
    items: list = [title]
    for block in fit([defang(problems), defang(environment), defang(mods)], TEXT_LIMIT - len(title) - len(footer)):
        items += [discord.ui.Separator(), block]
    items.append(footer)
    return card(*items, color=colour)


# ── Cog ───────────────────────────────────────────────────────────────────────

class LogParserCog(commands.Cog):
    """Automatically parses Minecraft Forge latest.log attachments.

    • High-confidence files (``latest.log``, ``debug.log``, crash reports) are
      parsed and replied to immediately.
    • Files that *look* like Minecraft logs but have a non-standard name get a
      🔍 reaction; an administrator must react with the same emoji to trigger
      the analysis.
    • Files that do **not** look like Minecraft logs are silently ignored, even
      if they have a ``.log`` / ``.txt`` extension.
    """

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        # Maps message-id → list of attachment URLs that are pending admin approval.
        # Cleared once the reaction is received or after the message is too old.
        self._pending: dict[int, list[str]] = {}
        self._parse_state = ModerationState()
        self._parse_events: dict[tuple[int, int], list[float]] = defaultdict(list)

    def _may_auto_parse(self, user_id: int) -> bool:
        """A few log reports per user per minute: ten latest.log files per message shouldn't mean ten bot replies."""
        events = self._parse_state.record(self._parse_events, (user_id, 0), time.monotonic(), AUTO_PARSE_WINDOW_SECONDS)
        return len(events) <= AUTO_PARSE_MAX_PER_WINDOW

    # ── on_message: detect & triage ───────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        pending_urls: list[str] = []

        for attachment in message.attachments:
            filename = attachment.filename.lower()
            if not any(filename.endswith(ext) for ext in ALLOWED_EXTENSIONS):
                continue

            if attachment.size > MAX_FILE_SIZE:
                log.warning(
                    "Skipping oversized attachment %s (%d bytes) from %s",
                    attachment.filename,
                    attachment.size,
                    message.author,
                )
                continue

            # Read a small preview to decide if this is a Minecraft log at all.
            try:
                raw_bytes = await attachment.read()
                text = raw_bytes.decode("utf-8", errors="replace")
            except Exception:
                log.exception("Failed to read attachment %s", attachment.filename)
                continue

            # A dmzdebug dump is handled by DmzDebugCog; skip it here so a player
            # state file (whose VisitedDimensions can contain "minecraft:...")
            # doesn't also get parsed as a Minecraft log.
            if looks_like_dmzdebug(text):
                continue

            if not _looks_like_mc_log(text):
                # Not a MC log → ignore completely, even if .log/.txt
                continue

            # ── High-confidence name → auto-parse immediately ─────────────
            if _is_high_confidence_name(attachment.filename):
                log.info(
                    "Auto-parsing %s uploaded by %s in #%s",
                    attachment.filename,
                    message.author,
                    getattr(message.channel, "name", "DM"),
                )
                if not self._may_auto_parse(message.author.id):
                    continue
                async with message.channel.typing():
                    # Off the event loop: a multi-MB log still takes about a second to parse.
                    report = await asyncio.to_thread(parse_log, text)
                    view = _build_card(report, attachment.filename)
                await message.reply(view=view, mention_author=False)
            else:
                # ── Uncertain name → queue for admin approval ─────────────
                pending_urls.append(attachment.url)

        # If any attachments need approval, add the reaction and store state.
        if pending_urls:
            self._pending[message.id] = pending_urls
            try:
                await message.add_reaction(_APPROVE_EMOJI)
            except discord.HTTPException:
                log.warning("Could not add approval reaction to message %s", message.id)

    # ── on_reaction_add: admin approval ───────────────────────────────────────

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.User | discord.Member):
        # Ignore bot's own reactions and DMs.
        if user.bot:
            return
        if not isinstance(user, discord.Member):
            return

        # Must be the approval emoji on a message we are tracking.
        if str(reaction.emoji) != _APPROVE_EMOJI:
            return
        if reaction.message.id not in self._pending:
            return

        # Only administrators may approve.
        if not is_admin(user):
            return

        urls = self._pending.pop(reaction.message.id, [])
        if not urls:
            return

        message = reaction.message

        for url in urls:
            # Find the matching attachment by URL.
            attachment = next(
                (a for a in message.attachments if a.url == url), None
            )
            if attachment is None:
                continue

            try:
                raw_bytes = await attachment.read()
                text = raw_bytes.decode("utf-8", errors="replace")
            except Exception:
                log.exception("Failed to read attachment %s on approval", attachment.filename)
                continue

            log.info(
                "Admin %s approved parsing of %s in #%s",
                user,
                attachment.filename,
                getattr(message.channel, "name", "DM"),
            )

            async with message.channel.typing():
                report = parse_log(text)
                view = _build_card(report, attachment.filename)
            await message.reply(view=view, mention_author=False)


# ── Detection helper ──────────────────────────────────────────────────────────

def _looks_like_mc_log(text: str) -> bool:
    """Return True if the first portion of *text* contains Minecraft-related keywords."""
    indicators = (
        "minecraft",
        "forge",
        "modlauncher",
        "fabricloader",
        "net.minecraftforge",
        "cpw.mods",
        "[main/info]",
        "[main/debug]",
        "[render thread/",
        "[server thread/",
    )
    lower = text[:8000].lower()
    return any(ind in lower for ind in indicators)


def setup(bot: discord.Bot):
    bot.add_cog(LogParserCog(bot))
