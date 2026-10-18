"""Log corpus collector: each crash log the log_parser cog analyzes in the main guild (tickets included) is saved
redacted with the chat before it, and announced in log_corpus_channel_id with an admin Exclude/Include toggle. Once a
DragonMineZ version has log_corpus_ping_threshold logs waiting, Bruno is pinged with a Download .zip button; the
download (that button, for Bruno or admins, or /log-corpus, Bruno only) adds the chat after each log, posts the
.zip(s) in the same channel and deletes the bot's copies. services/log_corpus holds the storage and the zips."""

import asyncio
import json
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands

from bulmaai.services import log_corpus as corpus
from bulmaai.services import log_pages
from bulmaai.ui.log_corpus_views import (
    EXCLUDE,
    ZIP,
    bundle_card,
    notice_card,
    parse_custom_id,
    ping_card,
    ping_done_card,
)
from bulmaai.ui.v2 import while_busy
from bulmaai.utils.permissions import BRUNO_ID, is_admin, is_allowed_guild_id, is_bruno

log = logging.getLogger(__name__)

DEFAULT_FILESIZE_LIMIT = 10 * 1024 * 1024
ADMIN_ONLY_MSG = "Only admins can exclude or include corpus logs."
DOWNLOAD_DENIED_MSG = "Only Bruno or an admin can download the log corpus."
BRUNO_ONLY_MSG = "Only Bruno can use this."
GONE_MSG = "That log was already downloaded or has expired."
DOWNLOAD_FAILED_MSG = "Couldn't build or upload the .zip. Nothing was marked as downloaded; try again."
OFF_MSG = "The log corpus is off (no log corpus channel is set)."
NOTHING_MSG = "No logs are waiting{scope}."


def _jump_url(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


class LogCorpusCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._tasks: set[asyncio.Task] = set()
        self._working: set[int] = set()  # ping cards with a click in flight (ui/v2.while_busy)
        self._bundling = asyncio.Lock()

    # ── Collecting ────────────────────────────────────────────────────────────

    def enabled_for(self, message: discord.Message) -> bool:
        settings = self.bot.settings
        return bool(getattr(settings, "log_corpus_channel_id", None)) and is_allowed_guild_id(
            getattr(message.guild, "id", None), settings
        )

    def collect_later(self, message: discord.Message, text: str, filename: str, report, *, channel_kind: str) -> None:
        """Fire and forget from the log_parser cog: failures are logged and never touch the log card."""
        if not self.enabled_for(message):
            return
        task = asyncio.create_task(self.collect(message, text, filename, report, channel_kind=channel_kind))
        self._tasks.add(task)
        task.add_done_callback(self._collected)

    def _collected(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("Log corpus collection failed", exc_info=task.exception())

    async def collect(self, message: discord.Message, text: str, filename: str, report, *, channel_kind: str) -> bool:
        """Saves one analyzed log, posts its notice and pings Bruno when its version reached the threshold.
        False when the collector is off here or this message + file is already in the corpus."""
        if not self.enabled_for(message):
            return False
        names = log_pages.player_names(text)
        stored = corpus.safe_filename(filename, names)
        version = corpus.version_key(report.dragonminez_version)
        if not await corpus.insert_entry(message.id, stored, message.channel.id, message.guild.id, version):
            return False
        try:
            context = corpus.metadata(
                report, filename=stored, posted_at=message.created_at, channel_kind=channel_kind, names=names
            )
            context["uploader_text"] = corpus.scrub(message.content or "", names)
            context["before"] = await self._before(message, names)
            context[corpus.HIDDEN_NAMES_KEY] = corpus.hide_names(names)
            redacted = await asyncio.to_thread(log_pages.redact, text)
            await asyncio.to_thread(corpus.write_files, message.id, stored, redacted, context)
        except Exception:
            await corpus.delete_entry(message.id, stored)
            corpus.delete_files([(message.id, stored)])
            raise
        try:
            await self._post_notice(message, stored, context)
        except discord.HTTPException:
            log.exception("Could not post the log corpus notice", extra={"message_id": message.id})
        await self.maybe_ping(version)
        return True

    async def _before(self, message: discord.Message, names) -> list[dict]:
        uploader_id = message.author.id
        since = message.created_at - corpus.BEFORE_WINDOW
        picked: list[dict] = []
        async for earlier in message.channel.history(
            limit=corpus.HISTORY_SCAN, before=message, after=since, oldest_first=False
        ):
            if earlier.author.bot:
                continue
            picked.append(
                corpus.context_message(earlier, uploader_id=uploader_id, settings=self.bot.settings, names=names)
            )
            if len(picked) >= corpus.BEFORE_LIMIT:
                break
        return picked[::-1]

    def _filesize_limit(self, guild=None) -> int:
        if guild is None:
            channel = self.bot.get_channel(self.bot.settings.log_corpus_channel_id or 0)
            guild = getattr(channel, "guild", None)
        limit = getattr(guild, "filesize_limit", None)
        return limit if isinstance(limit, int) and limit > 0 else DEFAULT_FILESIZE_LIMIT

    def _log_file(self, message_id: int, filename: str, guild) -> list[discord.File]:
        path = corpus.log_path(message_id, filename)
        if path.stat().st_size > self._filesize_limit(guild):
            return []
        return [discord.File(path, filename=path.name)]

    async def _post_notice(self, message: discord.Message, stored: str, context: dict) -> None:
        files = self._log_file(message.id, stored, None)
        view = notice_card(
            context,
            jump_url=_jump_url(message.guild.id, message.channel.id, message.id),
            message_id=message.id,
            key=corpus.file_key(stored),
            attachment=files[0].filename if files else None,
            excluded=False,
        )
        channel = self.bot.get_partial_messageable(self.bot.settings.log_corpus_channel_id)
        sent = await channel.send(view=view, files=files, allowed_mentions=discord.AllowedMentions.none())
        await corpus.set_notice(message.id, stored, sent.id)

    async def maybe_ping(self, version: str) -> bool:
        """Pings Bruno once per version when its waiting logs reach the threshold (0 = never); a bundle re-arms it."""
        settings = self.bot.settings
        threshold = int(getattr(settings, "log_corpus_ping_threshold", 0) or 0)
        channel_id = settings.log_corpus_channel_id
        if threshold <= 0 or not channel_id:
            return False
        count = await corpus.pending_count(version)
        if count < threshold or not await corpus.claim_ping(version):
            return False
        try:
            await self.bot.get_partial_messageable(channel_id).send(
                view=ping_card(count, version, BRUNO_ID),
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, replied_user=False, users=[discord.Object(id=BRUNO_ID)]
                ),
            )
        except discord.HTTPException:
            await corpus.clear_pings([version])
            raise
        return True

    # ── Bundling ──────────────────────────────────────────────────────────────

    async def _after(self, entry: corpus.Entry, hidden: dict | None) -> list[dict] | None:
        """Up to AFTER_LIMIT people's messages within AFTER_WINDOW after the log, read now; None when the channel,
        thread or log message is gone."""
        try:
            channel = self.bot.get_channel(entry.channel_id) or await self.bot.fetch_channel(entry.channel_id)
            original = await channel.fetch_message(entry.message_id)
        except (discord.NotFound, discord.Forbidden):
            return None
        until = original.created_at + corpus.AFTER_WINDOW
        picked: list[dict] = []
        async for later in channel.history(limit=corpus.HISTORY_SCAN, after=original, oldest_first=True):
            if later.created_at > until:
                break
            if later.author.bot:
                continue
            picked.append(
                corpus.context_message(
                    later,
                    uploader_id=original.author.id,
                    settings=self.bot.settings,
                    hidden=hidden,
                    log_message_id=entry.message_id,
                )
            )
            if len(picked) >= corpus.AFTER_LIMIT:
                break
        return picked

    async def _bundle_item(self, entry: corpus.Entry) -> corpus.BundleItem | None:
        try:
            log_bytes = await asyncio.to_thread(corpus.log_path(entry.message_id, entry.filename).read_bytes)
            context = await asyncio.to_thread(corpus.read_context, entry.message_id, entry.filename)
        except FileNotFoundError:
            log.info("Log corpus files missing, skipping", extra={"message_id": entry.message_id})
            return None
        after = await self._after(entry, context.pop(corpus.HIDDEN_NAMES_KEY, None))
        context["after"] = after or []
        context["after_available"] = after is not None
        return corpus.BundleItem(
            folder=corpus.safe_filename(entry.dmz_version),
            stem=entry.stem,
            log=log_bytes,
            context=json.dumps(context, ensure_ascii=False, indent=2).encode("utf-8"),
        )

    async def bundle(self, version: str | None) -> tuple[int, int]:
        """Posts every waiting log of version (all versions when None) as .zip(s) in the corpus channel, then marks
        them bundled, deletes their files and re-arms the ping. Returns (logs, zips); (0, 0) when nothing waits. Raises
        when building or uploading fails, after removing any part already posted, with nothing marked."""
        channel_id = self.bot.settings.log_corpus_channel_id
        if not channel_id:
            raise RuntimeError("log_corpus_channel_id is not set")
        async with self._bundling:
            entries = await corpus.pending_entries(version)
            if not entries:
                return 0, 0
            items = [item for item in [await self._bundle_item(entry) for entry in entries] if item is not None]
            zips = await asyncio.to_thread(corpus.build_zips, items, self._filesize_limit()) if items else []
            posted: list[discord.Message] = []
            channel = self.bot.get_partial_messageable(channel_id)
            today = datetime.now(timezone.utc)
            try:
                for number, (path, part) in enumerate(zips, 1):
                    name = corpus.zip_name(version, number, len(zips), today=today)
                    posted.append(
                        await channel.send(
                            view=bundle_card(version or "(all versions)", len(part), number, len(zips), name),
                            file=discord.File(path, filename=name),
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    )
            except Exception:
                for message in posted:
                    try:
                        await message.delete()
                    except discord.HTTPException:
                        log.info("Could not remove a partial log corpus zip", extra={"message_id": message.id})
                raise
            finally:
                for path, _part in zips:
                    path.unlink(missing_ok=True)
            await corpus.mark_bundled(entries)
            await asyncio.to_thread(corpus.delete_files, [(entry.message_id, entry.filename) for entry in entries])
            await corpus.clear_pings({entry.dmz_version for entry in entries})
            return len(items), len(zips)

    # ── Buttons ───────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        parsed = parse_custom_id((interaction.data or {}).get("custom_id") or "")
        if parsed is None:
            return
        action, message_id, value = parsed
        if action == ZIP:
            return await self._download_clicked(interaction, value)
        await self._toggle_clicked(interaction, message_id, value, excluded=action == EXCLUDE)

    async def _toggle_clicked(self, interaction: discord.Interaction, message_id: int, key: str, *, excluded: bool):
        settings = self.bot.settings
        if not is_admin(interaction.user, settings=settings):
            return await interaction.response.send_message(ADMIN_ONLY_MSG, ephemeral=True)
        entry = await corpus.find_entry(message_id, key)
        if entry is None or entry.bundled_at is not None:
            return await interaction.response.send_message(GONE_MSG, ephemeral=True)
        try:
            context = await asyncio.to_thread(corpus.read_context, entry.message_id, entry.filename)
            files = self._log_file(entry.message_id, entry.filename, interaction.guild)
        except FileNotFoundError:
            return await interaction.response.send_message(GONE_MSG, ephemeral=True)
        updated = await corpus.set_excluded(entry.message_id, entry.filename, excluded)
        if updated is None:
            return await interaction.response.send_message(GONE_MSG, ephemeral=True)
        view = notice_card(
            context,
            jump_url=_jump_url(entry.guild_id, entry.channel_id, entry.message_id),
            message_id=entry.message_id,
            key=entry.key,
            attachment=files[0].filename if files else None,
            excluded=excluded,
        )
        # The log is uploaded again with the edit, so the card's file component always has its attachment.
        await interaction.response.edit_message(view=view, files=files, attachments=[])
        if not excluded:
            try:
                await self.maybe_ping(entry.dmz_version)
            except Exception:
                log.exception("Log corpus ping failed", extra={"version": entry.dmz_version})

    async def _download_clicked(self, interaction: discord.Interaction, version: str):
        user = interaction.user
        if not (is_bruno(user) or is_admin(user, settings=self.bot.settings)):
            return await interaction.response.send_message(DOWNLOAD_DENIED_MSG, ephemeral=True)

        async def work() -> bool:
            logs, _zips = await self.bundle(version)
            await interaction.edit_original_response(view=ping_done_card(version, logs))
            if not logs:
                await interaction.followup.send(NOTHING_MSG.format(scope=f" for {version}"), ephemeral=True)
            return True

        await while_busy(interaction, self._working, work, failed=DOWNLOAD_FAILED_MSG)

    # ── /log-corpus ───────────────────────────────────────────────────────────

    @discord.slash_command(name="log-corpus", description="Post the collected crash logs as a .zip (Bruno only)")
    @discord.option("version", str, description="DragonMineZ version; empty: every version", required=False)
    async def log_corpus_command(self, ctx: discord.ApplicationContext, version: str | None = None):
        if not is_bruno(ctx.author):
            return await ctx.respond(BRUNO_ONLY_MSG, ephemeral=True)
        if not self.bot.settings.log_corpus_channel_id:
            return await ctx.respond(OFF_MSG, ephemeral=True)
        version = (version or "").strip() or None
        await ctx.defer(ephemeral=True)
        try:
            logs, zips = await self.bundle(version)
        except Exception:
            log.exception("Log corpus download failed", extra={"version": version})
            return await ctx.followup.send(DOWNLOAD_FAILED_MSG, ephemeral=True)
        if not logs:
            scope = f" for {version}" if version else ""
            return await ctx.followup.send(NOTHING_MSG.format(scope=scope), ephemeral=True)
        channel_id = self.bot.settings.log_corpus_channel_id
        await ctx.followup.send(f"Posted {logs} logs in {zips} .zip file(s) in <#{channel_id}>.", ephemeral=True)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(LogCorpusCog(bot))
