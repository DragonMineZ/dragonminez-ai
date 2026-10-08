import asyncio
import hashlib
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks
from requests import HTTPError

from bulmaai.github.github_app_auth import GitHubAppAuth
from bulmaai.github.github_service import GitHubService
from bulmaai.services.patch_notes import (
    PatchNotesState,
    build_patch_notes_url,
    get_patch_notes_state,
    pick_latest_patch_notes,
    summarize_patch_notes_update,
    upsert_patch_notes_state,
)
from bulmaai.utils.lifecycle import ReloadableCog
from bulmaai.ui.v2 import card, trim

log = logging.getLogger(__name__)

PATCH_NOTES_POLL_MINUTES = 15
PATCH_NOTES_COLOR = discord.Colour.from_rgb(46, 204, 113)


def build_patch_notes_update_card(
    *,
    summary: str,
    updated_at: datetime,
    patch_notes_url: str,
) -> discord.ui.DesignerView:
    day = f"{updated_at:%B %d, %Y}"
    return card(
        f"## 📝 [Patch Notes Updated]({patch_notes_url})\n"
        f"The daily 9 AM patch notes routine has finished and the patch notes for **{day}** are live.",
        discord.ui.Separator(),
        f"### ✨ What's new\n{trim(summary, 3000)}",
        f"-# DragonMineZ Patch Notes · {discord.utils.format_dt(updated_at, 'f')}",
        color=PATCH_NOTES_COLOR,
        buttons=[discord.ui.Button(label="Read the Patch Notes", url=patch_notes_url)],
    )


class PatchNotesUpdatesCog(ReloadableCog):
    """Watches the patch notes branch and announces the daily 9 AM update."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.gh = self._build_github_service()
        self._poll_lock = asyncio.Lock()

    @property
    def settings(self):
        return self.bot.settings

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
            repo=settings.patch_notes_repo,
        )

    async def on_startup(self) -> None:
        if self.poll_patch_notes.is_running():
            return
        if self.gh is None:
            log.warning("GitHub App credentials missing; patch notes updates will not be watched.")
            return
        self.poll_patch_notes.start()
        log.info("Patch notes polling loop started (every %s min).", PATCH_NOTES_POLL_MINUTES)

    async def on_shutdown(self) -> None:
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
        # Read fresh from bot.settings so a panel settings change (which calls
        # reload_settings) repoints the file on the next poll without a restart.
        branch = self.bot.settings.patch_notes_branch
        # State is keyed by the configured path (usually the PATCH_NOTES folder), so
        # a new latest file (v2.1.1 -> v2.2) is announced as an update, not re-seeded.
        configured_path = self.bot.settings.patch_notes_file_path
        try:
            if configured_path.lower().endswith(".md"):
                latest_path = configured_path
            else:
                latest_path = pick_latest_patch_notes(await self.gh.list_dir(configured_path, ref=branch))
            if latest_path is None:
                log.info("No patch notes in %s@%s; skipping.", configured_path, branch)
                return
            content, _blob_sha = await self.gh.get_file(latest_path, ref=branch)
        except HTTPError as error:
            if error.response is not None and error.response.status_code == 404:
                log.info("Patch notes %s@%s not found; skipping.", configured_path, branch)
                return
            raise
        content_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()

        previous = await get_patch_notes_state(branch, configured_path)
        if previous is not None and previous.content_sha == content_sha:
            return

        await upsert_patch_notes_state(
            PatchNotesState(
                branch=branch,
                file_path=configured_path,
                content_sha=content_sha,
                content=content,
            )
        )

        if previous is None:
            log.info("First patch notes run; seeding state without announcing.")
            return

        summary = summarize_patch_notes_update(previous.content, content)
        await self._announce_update(summary, latest_path)

    async def _announce_update(self, summary: str, file_path: str) -> None:
        patch_notes_url = build_patch_notes_url(
            self.bot.settings.patch_notes_repo,
            self.bot.settings.patch_notes_branch,
            file_path,
        )
        view = build_patch_notes_update_card(
            summary=summary,
            updated_at=datetime.now(timezone.utc),
            patch_notes_url=patch_notes_url,
        )
        for channel_id in self.bot.settings.dev_jar_announcement_channel_ids:
            try:
                channel = self.bot.get_channel(channel_id)
                if channel is None:
                    channel = await self.bot.fetch_channel(channel_id)
                if not hasattr(channel, "send"):
                    log.error("Configured patch notes channel %s is not messageable", channel_id)
                    continue
                await channel.send(
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception:
                log.exception("Failed to announce patch notes update to channel %s", channel_id)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(PatchNotesUpdatesCog(bot))
