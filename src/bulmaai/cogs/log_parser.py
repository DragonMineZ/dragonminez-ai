import asyncio
import logging
import re
import time
from collections import defaultdict

import discord
from discord.ext import commands, tasks

from bulmaai.services import ai_budget, log_ai_quota, log_corpus, log_pages
from bulmaai.services.ai_client import ConversationMessage, run_support_agent
from bulmaai.services.ai_guard import NO_REPLY, can_post_publicly, defang, safe_name
from bulmaai.services.moderation import ModerationState
from bulmaai.utils.dmz_addons import check_addons
from bulmaai.utils.dmzdebug_parser import looks_like_dmzdebug
from bulmaai.utils.forwards import full_attachments
from bulmaai.utils.language import detect_language_from_text
from bulmaai.utils.log_parser import LogReport, RootCause, parse_log
from bulmaai.utils.permissions import has_any_allowed_role, is_admin, is_allowed_guild_id, is_staff
from bulmaai.ui.v2 import TEXT_LIMIT, card, fit, trim

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

AI_HEADER = "**🤖 AI diagnosis**"
AI_FOOTER = "-# Bulma is AI and can make mistakes."
# Below this many characters the uploader's message says too little to pick a language from.
_MIN_LANGUAGE_TEXT = 15
_AI_CONTEXT_LINES = (
    "channel: public crash-log analysis card; your reply is added to the bot's log analysis under \"AI diagnosis\"",
    "task: give the most likely cause and concrete fix steps in a few short lines. No greeting. If the analysis "
    "doesn't show enough to tell, say so plainly and name what would help (e.g. the crash report or debug.log). "
    "Never ask questions back. Mark the reply kind answer, also when the log isn't enough.",
)

# Strips "[24Jun2023 06:57:42.886] [Render thread/FATAL] [net.minecraftforge.ForgeMod/]:" (or the logger-less
# "[12:00:00] [main/ERROR]:") from the front of raw log lines so only the human-readable message is shown.
_RE_STRIP_LOG_PREFIX = re.compile(
    r"^\[[^]]+][ \t]*\[[^]]+](?:[ \t]*\[[^]]*])?:?[ \t]*"
)

SUMMARY_LIMIT = 1500
_SUMMARY_MODS_LIMIT = 400
_SUMMARY_CHAIN_LIMIT = 700
CAUSE_HEADER = "**🎯 Likely cause**"
MIXIN_HINT = "-# Usually fixed by updating or removing that mod, or reporting it to its author."


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
        # Skip frame lines, they're noise in a short card snippet; our own frames say where it broke.
        if stripped.startswith("...") or (stripped.startswith("at ") and "com.dragonminez" not in stripped):
            continue
        keep.append(stripped)
        if len(keep) >= 10:
            break
    return "\n".join(keep)


def _in_ticket_channel(channel, settings) -> bool:
    """The open-tickets category, as ai_tickets._is_ticket_channel checks it (importing that cog here would be circular)."""
    category_id = getattr(channel, "category_id", None)
    return category_id is not None and category_id == settings.ai_ticket_category_id


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

def _culprit(cause: RootCause, code) -> str:
    """'`noeabosses` (`Noea.jar`)', or whichever of the two is known; code wraps each name."""
    if cause.culprit_mod and cause.culprit_jar:
        return f"{code(cause.culprit_mod)} ({code(cause.culprit_jar)})"
    return code(cause.culprit_mod or cause.culprit_jar or "")


def _code(text: str) -> str:
    return "`" + text.replace("`", "'") + "`"


def _mod_names(mods: dict[str, str], limit: int) -> str:
    """'Mods (N): a, b, c, +M more' within limit characters."""
    names = sorted(mods)
    text = f"Mods ({len(names)}): "
    for i, name in enumerate(names):
        piece = (", " if i else "") + name
        # Room for the "+N more" tail is kept at every step, so cutting off here still fits.
        if len(text) + len(piece) + len(f", +{len(names) - i - 1} more") > limit:
            return text + (", " if i else "") + f"+{len(names) - i} more"
        text += piece
    return text


def summary_for_ai(report: LogReport) -> str:
    """The analysis as plain text for the support AI; the raw log never goes to the model. The likely cause and the
    exception chain come first and are never cut for the error or mod lists."""
    env = ", ".join(
        f"{name} {value}"
        for name, value in (
            ("Minecraft", report.mc_version), ("Forge", report.forge_version), ("Java", report.java_version),
            ("DragonMineZ", report.dragonminez_version), ("OS", report.operating_system),
        )
        if value
    )
    head = [f"Environment: {env or 'not found in this file'}"]
    cause = report.root_cause
    if cause:
        if cause.culprit_mod or cause.culprit_jar:
            what = "mixin failure" if cause.kind == "mixin" else "crash"
            head.append(f"Likely cause: {what} in {_culprit(cause, str)}")
        # The deepest cause keeps its longer text: it's the line that usually names the problem.
        chain = [*cause.chain[:-1], cause.exception]
        head.append(trim("Exception chain (deepest last):\n" + "\n".join(f"- {c}" for c in chain), _SUMMARY_CHAIN_LIMIT))
    elif report.stacktrace and (summary := _summarise_stacktrace(report.stacktrace)):
        head.append("Exception summary:\n" + _truncate(summary, _SUMMARY_CHAIN_LIMIT))

    tail = []
    bad_addons = [a for a in check_addons(report.mods, report.dragonminez_version) if a.compatible is False]
    if bad_addons:
        tail.append(trim("Incompatible DMZ addons: " + ", ".join(
            f"{a.mod_id} {a.version} (needs DMZ >= {a.min_dmz_version})" for a in bad_addons
        ), 300))
    tail.append(_mod_names(report.mods, _SUMMARY_MODS_LIMIT) if report.mods else "Mod list: not in this file.")

    # Errors get whatever room the cause and the mod list leave.
    room = SUMMARY_LIMIT - sum(len(line) + 1 for line in head + tail)
    errors: list[str] = []
    if report.errors:
        header = f"Errors ({len(report.errors)}):"
        room -= len(header) + 1
        for error in report.errors[:6]:
            line = f"- {_truncate(_clean_error_line(error), 200)}"
            if len(line) + 1 > room:
                break
            errors.append(line)
            room -= len(line) + 1
        if errors:
            errors.insert(0, header)
    else:
        errors.append("No errors after filtering known harmless noise.")
    return trim("\n".join(head + errors + tail), SUMMARY_LIMIT)


def _cause_block(cause: RootCause | None) -> str:
    """The card's likely-cause block: shown when a mod was found or the deepest cause differs from the top
    exception (which the Exception Summary already shows)."""
    if cause is None:
        return ""
    culprit = cause.culprit_mod or cause.culprit_jar
    if not culprit and len(cause.chain) < 2:
        return ""
    lines = [CAUSE_HEADER]
    if culprit:
        where = _culprit(cause, _code)
        lines.append(f"Mixin injection failed in {where}" if cause.kind == "mixin" else f"The crash points to {where}")
    lines.append(_code(cause.exception))
    if culprit and cause.kind == "mixin":
        lines.append(MIXIN_HINT)
    return "\n".join(lines)


def _limit_note(limit: int, patron_limit: int, *, patron: bool) -> str:
    if patron:
        return f"-# 🤖 Daily AI diagnosis limit reached ({limit}/day)."
    return f"-# 🤖 Daily AI diagnosis limit reached ({limit}/day; patrons get {patron_limit})."


def _build_card(
    report: LogReport,
    filename: str,
    page_url: str | None = None,
    *,
    diagnosis: str | None = None,
    limit_note: str | None = None,
) -> discord.ui.DesignerView:
    """The analysis as a V2 card: what went wrong first, then the AI diagnosis (or the limit note), then the
    environment, then the mod lists. Everything from the uploaded file is defanged (no live links or pings); the
    diagnosis is not, it was already screened by ai_guard and carries our wiki links."""
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
    likely_cause = _cause_block(report.root_cause)

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
        mods = "**🧩 Mods**\n*No mod list in this file (Forge's latest.log doesn't print one; debug.log and crash reports do).*"
    addons = check_addons(report.mods, report.dragonminez_version)
    if addons:
        mods += f"\n**🐉 DMZ Addons ({len(addons)})**\n" + "\n".join(
            f"⚠️ `{a.mod_id}` — {a.version} *(needs DMZ ≥ {a.min_dmz_version})*"
            if a.compatible is False
            else f"✅ `{a.mod_id}` — {a.version}"
            for a in addons
        )

    footer = f"-# 📄 {discord.utils.escape_markdown(_truncate(filename, 100))}"
    budget = TEXT_LIMIT - len(title) - len(footer)
    # Problems is never empty, so after fit drops empty blocks the diagnosis sits right after it and the cause.
    second = diagnosis or limit_note
    if diagnosis:
        budget -= len(AI_HEADER) + len(AI_FOOTER) + 2
    blocks = fit([defang(problems), defang(likely_cause), second or "", defang(environment), defang(mods)], budget)
    if diagnosis:
        at = 2 if likely_cause else 1
        blocks[at] = f"{AI_HEADER}\n{blocks[at]}\n{AI_FOOTER}"
    items: list = [title]
    for block in blocks:
        items += [discord.ui.Separator(), block]
    items.append(footer)
    buttons = [discord.ui.Button(label="Full log (24h)", emoji="📄", url=page_url)] if page_url else None
    return card(*items, color=colour, buttons=buttons)


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
        self.purge_log_pages.start()

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

        for attachment in full_attachments(message):
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

            if not looks_like_mc_log(text):
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
                    report, url = await self._analyze(text, attachment.filename)
                sent = await message.reply(view=_build_card(report, attachment.filename, url), mention_author=False)
                self._collect(message, text, attachment.filename, report)
                await self._add_diagnosis(sent, message, report, attachment.filename, url, approved=False)
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
                (a for a in full_attachments(message) if a.url == url), None
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
                report, url = await self._analyze(text, attachment.filename)
            sent = await message.reply(view=_build_card(report, attachment.filename, url), mention_author=False)
            self._collect(message, text, attachment.filename, report)
            await self._add_diagnosis(sent, message, report, attachment.filename, url, approved=True)

    def _collect(self, message: discord.Message, text: str, filename: str, report: LogReport) -> None:
        """Hands the analyzed log to the log corpus (cogs/log_corpus.py) in the background; never affects the card."""
        try:
            collector = self.bot.get_cog("LogCorpusCog")
            if collector is not None:
                kind = "ticket" if _in_ticket_channel(message.channel, self.bot.settings) else "public"
                collector.collect_later(message, text, filename, report, channel_kind=kind)
        except Exception:
            log.exception("Could not hand %s to the log corpus", filename)

    async def _add_diagnosis(
        self,
        sent: discord.Message,
        message: discord.Message,
        report: LogReport,
        filename: str,
        url: str | None,
        *,
        approved: bool,
    ) -> None:
        """Edits the posted card to add the AI diagnosis, or the daily-limit note. Any skip or failure leaves the
        card as posted; nothing is shown while the AI runs, so a restart mid-run can't leave a stuck card.
        Admin-approved (🔍) parses always run and don't count against the uploader."""
        settings = self.bot.settings
        author = message.author
        channel = message.channel
        if not (report.errors or report.stacktrace) or not getattr(settings, "log_ai_enabled", False):
            return
        if not is_allowed_guild_id(getattr(message.guild, "id", None), settings):
            return
        if _in_ticket_channel(channel, settings):
            return  # the ticket AI already reads logs posted in its channel
        if ai_budget.is_paused(settings):
            log.info("Skipping log AI diagnosis for %s: AI budget paused", filename)
            return

        counted = not approved and not is_staff(author, settings=settings)
        if counted:
            patron = has_any_allowed_role(author, settings.dev_jar_patreon_role_ids) or has_any_allowed_role(
                author, settings.dev_jar_tester_role_ids
            )
            limit = settings.log_ai_patron_daily_limit if patron else settings.log_ai_daily_limit
            try:
                left = await log_ai_quota.remaining(author.id, limit)
            except Exception:
                log.exception("Log AI quota lookup failed for %s", author.id)
                return
            if left <= 0:
                note = _limit_note(limit, settings.log_ai_patron_daily_limit, patron=patron)
                await self._edit_card(sent, _build_card(report, filename, url, limit_note=note))
                return

        content = (message.content or "").strip()
        language = detect_language_from_text(content) if len(content) >= _MIN_LANGUAGE_TEXT else "en"
        name = safe_name(getattr(author, "display_name", None) or author.name)
        try:
            result = await run_support_agent(
                messages=[
                    ConversationMessage(
                        role="user",
                        content=f"Log file: {filename}\n{summary_for_ai(report)}",
                        speaker_name=name,
                        speaker_id=str(author.id),
                        speaker_kind="requester",
                    )
                ],
                enabled_tools=[],
                language_hint=language,
                user_id=author.id,
                channel_id=channel.id,
                ticket_conversation=False,
                bot=self.bot,
                settings=settings,
                context_lines=[*_AI_CONTEXT_LINES, f"requester: {name}"],
                requester_is_staff=is_staff(author, settings=settings),
                channel_kind="public",
                workflow_prefix="log_diagnosis",
            )
        except Exception:
            log.exception("Log AI diagnosis failed for %s", filename)
            return
        if result.get("paused"):
            log.info("Skipping log AI diagnosis for %s: AI budget paused", filename)
            return
        if counted:
            try:
                await log_ai_quota.record(author.id)
            except Exception:
                log.exception("Recording log AI use failed for %s", author.id)
        reply = (result.get("reply") or "").strip()
        if not reply or reply == NO_REPLY or not can_post_publicly(result):
            log.info(
                "Log AI diagnosis for %s not posted (kind=%s, flags=%s)",
                filename,
                result.get("kind"),
                ",".join(sorted(result.get("guard_flags") or ())) or "-",
            )
            return
        await self._edit_card(sent, _build_card(report, filename, url, diagnosis=reply))

    @staticmethod
    async def _edit_card(sent: discord.Message, view: discord.ui.DesignerView) -> None:
        try:
            await sent.edit(view=view)
        except discord.HTTPException:
            log.info("Could not edit log card %s (deleted?)", sent.id)

    async def _analyze(self, text: str, filename: str) -> tuple[LogReport, str | None]:
        # Off the event loop: a multi-MB log still takes about a second to parse and render.
        report = await asyncio.to_thread(parse_log, text)
        url = None
        try:
            marks = [(n, _clean_error_line(e)) for n, e in zip(report.error_line_numbers, report.errors)]
            facts = [
                ("Minecraft", report.mc_version or ""), ("Forge", report.forge_version or ""),
                ("Java", report.java_version or ""), ("DragonMineZ", report.dragonminez_version or ""),
            ]
            page = await asyncio.to_thread(
                lambda: log_pages.render(log_pages.redact(text), title=filename, kind="log", facts=facts, marks=marks)
            )
            url = await log_pages.save(self.bot.settings, page)
        except Exception:
            log.exception("Failed to publish log page %s", filename)
        return report, url

    @tasks.loop(hours=1)
    async def purge_log_pages(self) -> None:
        removed = await asyncio.to_thread(log_pages.purge, self.bot.settings)
        if removed:
            log.info("Purged %d expired log pages", removed)
        try:
            await log_ai_quota.purge()
        except Exception:
            log.exception("Purging old log AI uses failed")
        try:
            if expired := await log_corpus.purge_expired():
                log.info("Purged %d expired log corpus entries", expired)
        except Exception:
            log.exception("Purging the log corpus failed")

    def cog_unload(self) -> None:
        self.purge_log_pages.cancel()


# ── Detection helper ──────────────────────────────────────────────────────────

def looks_like_mc_log(text: str) -> bool:
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
