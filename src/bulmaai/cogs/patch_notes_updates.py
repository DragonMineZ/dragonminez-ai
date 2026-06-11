import asyncio
import hashlib
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

from bulmaai.cogs.dev_jar_downloads import DEV_JAR_ANNOUNCEMENT_CHANNEL_IDS
from bulmaai.github.github_app_auth import GitHubAppAuth
from bulmaai.github.github_service import GitHubService
from bulmaai.services.patch_notes import (
    PATCH_NOTES_BRANCH,
    PATCH_NOTES_FILE_PATH,
    PATCH_NOTES_REPO,
    PATCH_NOTES_URL,
    PatchNotesState,
    get_patch_notes_state,
    summarize_patch_notes_update,
    upsert_patch_notes_state,
)

log = logging.getLogger(__name__)

PATCH_NOTES_POLL_MINUTES = 15
PATCH_NOTES_EMBED_COLOR = discord.Colour.from_rgb(46, 204, 113)


def build_patch_notes_update_embed(
    *,
    summary: str,
    updated_at: datetime,
) -> discord.Embed:
    day = f"{updated_at:%B %d, %Y}"
    embed = discord.Embed(
        title="DragonMineZ Patch Notes Updated",
        url=PATCH_NOTES_URL,
        description=(
            f"The daily 9 AM patch notes routine has finished and the v2.1 patch notes "
            f"for {day} are live. Read the full document here:\n{PATCH_NOTES_URL}"
        ),
        colour=PATCH_NOTES_EMBED_COLOR,
        timestamp=updated_at,
    )
    embed.add_field(name="What's new", value=summary[:1024], inline=False)
    embed.set_footer(text="DragonMineZ Patch Notes")
    return embed


class PatchNotesUpdateView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="Read the Patch Notes", url=PATCH_NOTES_URL))


class PatchNotesUpdatesCog(commands.Cog):
    """Watches the patch notes branch and announces the daily 9 AM update."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.settings = bot.settings
        self.gh = self._build_github_service()
        self._poll_lock = asyncio.Lock()
        self._poll_started = False

    def _build_github_service(self) -> GitHubService | None:
        settings = self.bot.settings
        if not settings.GH_APP_ID or not settings.GH_INSTALLATION_ID or not settings.GH_APP_PRIVATE_KEY_PEM:
            return None
        auth = GitHubAppAuth(
            app_id=settings.GH_APP_ID,
            installation_id=settings.GH_INSTALLATION_ID,
            private_key_pem=settings.GH_APP_PRIVATE_KEY_PEM.replace("\\n", "\n"),
        )
        return GitHubService(
            auth=auth,
            owner=settings.GITHUB_OWNER,
            repo=PATCH_NOTES_REPO,
        )

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._poll_started or self.poll_patch_notes.is_running():
            return
        if self.gh is None:
            log.warning("GitHub App credentials missing; patch notes updates will not be watched.")
            return
        self.poll_patch_notes.start()
        self._poll_started = True
        log.info("Patch notes polling loop started (every %s min).", PATCH_NOTES_POLL_MINUTES)

    def cog_unload(self) -> None:
        self.poll_patch_notes.cancel()

    @tasks.loop(minutes=PATCH_NOTES_POLL_MINUTES)
    async def poll_patch_notes(self) -> None:
        async with self._poll_lock:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Failed to poll patch notes branch")

    @poll_patch_notes.before_loop
    async def _before_poll(self) -> None:
        await self.bot.wait_until_ready()

    async def _poll_once(self) -> None:
        if self.gh is None:
            return
        content, _blob_sha = await self.gh.get_file(PATCH_NOTES_FILE_PATH, ref=PATCH_NOTES_BRANCH)
        content_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()

        previous = await get_patch_notes_state(PATCH_NOTES_BRANCH, PATCH_NOTES_FILE_PATH)
        if previous is not None and previous.content_sha == content_sha:
            return

        await upsert_patch_notes_state(
            PatchNotesState(
                branch=PATCH_NOTES_BRANCH,
                file_path=PATCH_NOTES_FILE_PATH,
                content_sha=content_sha,
                content=content,
            )
        )

        if previous is None:
            log.info("First patch notes run; seeding state without announcing.")
            return

        summary = summarize_patch_notes_update(previous.content, content)
        await self._announce_update(summary)

    async def _announce_update(self, summary: str) -> None:
        embed = build_patch_notes_update_embed(
            summary=summary,
            updated_at=datetime.now(timezone.utc),
        )
        for channel_id in DEV_JAR_ANNOUNCEMENT_CHANNEL_IDS:
            try:
                channel = self.bot.get_channel(channel_id)
                if channel is None:
                    channel = await self.bot.fetch_channel(channel_id)
                if not hasattr(channel, "send"):
                    log.error("Configured patch notes channel %s is not messageable", channel_id)
                    continue
                await channel.send(
                    embed=embed,
                    view=PatchNotesUpdateView(),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception:
                log.exception("Failed to announce patch notes update to channel %s", channel_id)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(PatchNotesUpdatesCog(bot))
