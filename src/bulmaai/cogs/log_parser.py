import logging
import re

import discord
from discord.ext import commands

from bulmaai.utils.dmz_addons import check_addons
from bulmaai.utils.dmzdebug_parser import looks_like_dmzdebug
from bulmaai.utils.log_parser import parse_log, LogReport
from bulmaai.utils.permissions import is_admin

log = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = (".log", ".txt")
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

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

    Drops all 'at com.mojang...' lines so the embed stays readable.
    Keeps at most 10 meaningful lines.
    """
    keep: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Skip frame lines — they're noise in a short embed snippet
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


# ── Embed builder ─────────────────────────────────────────────────────────────

def _build_embed(report: LogReport, filename: str) -> discord.Embed:
    """Construct a Discord embed from a parsed LogReport.

    Field value limits:  1 024 chars each (Discord hard limit).
    Total embed limit:   6 000 chars (Discord hard limit).
    We target well under both to stay safe.
    """
    has_errors = bool(report.errors)
    is_valid = report.is_forge or bool(report.mc_version)

    # Colour: red = errors present, orange = no Forge detected, green = clean
    if has_errors:
        colour = discord.Colour.red()
    elif not is_valid:
        colour = discord.Colour.orange()
    else:
        colour = discord.Colour.green()

    embed = discord.Embed(
        title="🔍 Log Analysis",
        colour=colour,
        timestamp=discord.utils.utcnow(),   # shows a clean timestamp in the footer
    )
    embed.set_footer(text=f"📄 {filename}")

    # ── Non-Forge warning ─────────────────────────────────────────────────────
    if not is_valid:
        embed.description = (
            "⚠️ This file does not appear to be a Minecraft Forge log.\n"
            "Results may be incomplete."
        )

    # ── Environment ───────────────────────────────────────────────────────────
    env_lines: list[str] = []
    if report.mc_version:
        env_lines.append(f"🎮 **Minecraft:** `{report.mc_version}`")
    if report.forge_version:
        env_lines.append(f"⚙️ **Forge:** `{report.forge_version}`")
    if report.java_version:
        env_lines.append(f"☕ **Java:** `{report.java_version}`")
    if report.dragonminez_version:
        env_lines.append(f"🐉 **DragonMineZ:** `{report.dragonminez_version}`")
    if report.operating_system:
        env_lines.append(f"💻 **OS:** {report.operating_system}")
    if report.memory:
        env_lines.append(f"🧠 **Memory:** {report.memory}")

    if env_lines:
        embed.add_field(
            name="🖥️ Environment",
            value="\n".join(env_lines),
            inline=False,
        )

    # ── Mods ──────────────────────────────────────────────────────────────────
    mod_count = len(report.mods)
    if mod_count:
        sorted_mods = sorted(report.mods.items())
        if mod_count <= 20:
            mods_text = "\n".join(
                f"`{mid}` — {ver}" for mid, ver in sorted_mods
            )
        else:
            shown = sorted_mods[:15]
            mods_text = "\n".join(f"`{mid}` — {ver}" for mid, ver in shown)
            mods_text += f"\n*…and **{mod_count - 15}** more*"

        embed.add_field(
            name=f"🧩 Mods Detected ({mod_count})",
            value=_truncate(mods_text, 900),
            inline=False,
        )
    else:
        embed.add_field(
            name="🧩 Mods Detected",
            value="*No mods detected. Log may be incomplete or vanilla.*",
            inline=False,
        )

    # ── DragonMineZ absent notice ─────────────────────────────────────────────
    if mod_count and not report.dragonminez_version:
        embed.add_field(
            name="ℹ️ DragonMineZ",
            value="Not detected among the loaded mods.",
            inline=False,
        )

    # ── DMZ addons ────────────────────────────────────────────────────────────
    addons = check_addons(report.mods, report.dragonminez_version)
    if addons:
        lines = []
        for a in addons:
            if a.compatible is False:
                lines.append(
                    f"⚠️ `{a.mod_id}` — {a.version} *(needs DMZ ≥ {a.min_dmz_version})*"
                )
            else:
                lines.append(f"✅ `{a.mod_id}` — {a.version}")
        embed.add_field(
            name=f"🐉 DMZ Addons ({len(addons)})",
            value=_truncate("\n".join(lines), 900),
            inline=False,
        )

    # ── Errors ────────────────────────────────────────────────────────────────
    if report.errors:
        cleaned = [_clean_error_line(e) for e in report.errors[:8]]
        errors_text = "\n".join(f"• {_truncate(e, 120)}" for e in cleaned)
        embed.add_field(
            name=f"❌ Errors / Fatal ({len(report.errors)})",
            value=_truncate(errors_text, 900),
            inline=False,
        )
    else:
        embed.add_field(
            name="✅ Status",
            value="No errors or fatal messages found.",
            inline=False,
        )

    # ── Stacktrace snippet ────────────────────────────────────────────────────
    if report.stacktrace:
        summary = _summarise_stacktrace(report.stacktrace)
        if summary:
            embed.add_field(
                name="📋 Exception Summary",
                value=f"```\n{_truncate(summary, 800)}\n```",
                inline=False,
            )

    return embed


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
                async with message.channel.typing():
                    report = parse_log(text)
                    embed = _build_embed(report, attachment.filename)
                await message.reply(embed=embed, mention_author=False)
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
                embed = _build_embed(report, attachment.filename)
            await message.reply(embed=embed, mention_author=False)


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
