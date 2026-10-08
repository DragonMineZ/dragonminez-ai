import asyncio
import logging
import math

import discord
from discord.ext import tasks

from bulmaai.services.curseforge_client import CurseForgeClient, CurseForgeRelease
from bulmaai.services.curseforge_state import (
    get_curseforge_project_state,
    upsert_curseforge_project_state,
)
from bulmaai.utils.lifecycle import ReloadableCog
from bulmaai.ui.v2 import card, facts, head

logger = logging.getLogger(__name__)

CURSEFORGE_COLOR = discord.Color.from_rgb(242, 100, 53)
MAX_CHANGELOG_CHARS = 900


def _truncate(text: str, limit: int = MAX_CHANGELOG_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0] + "..."


def _format_bytes(size_bytes: int | None) -> str:
    if size_bytes is None or size_bytes < 0:
        return "Unknown"

    if size_bytes == 0:
        return "0 B"

    units = ("B", "KB", "MB")
    index = min(int(math.log(size_bytes, 1024)), len(units) - 1)
    value = size_bytes / (1024 ** index)
    return f"{value:.1f} {units[index]}"


def _humanize_release_type(value: str) -> str:
    if not value:
        return "Unknown"
    return value.replace("_", " ").title()


def _build_release_card(release: CurseForgeRelease) -> discord.ui.DesignerView:
    uploaded = int(release.uploaded_at.timestamp())
    top = (
        f"-# [{release.project_title}](<{release.project_url}>) on CurseForge\n"
        f"## [{discord.utils.escape_markdown(release.file_display_name)}]({release.file_page_url})\n"
        + (release.project_summary.strip() if release.project_summary else "A new DragonMineZ file is available on CurseForge.")
    )
    details = "\n".join(
        line
        for line in (
            facts(
                ("Type", _humanize_release_type(release.release_type)),
                ("Size", _format_bytes(release.file_size_bytes)),
                ("Downloads", f"{release.download_count:,}" if release.download_count is not None else None),
            ),
            facts(
                ("Minecraft", ", ".join(release.minecraft_versions[:6])),
                ("Loaders", ", ".join(release.loader_tags[:6])),
                ("Tags", ", ".join(release.environment_tags[:6])),
            ),
            f"**Uploaded** <t:{uploaded}:F> (<t:{uploaded}:R>)　**File** `{release.file_name}`",
        )
        if line
    )
    changelog = f"### 📜 Changelog\n{_truncate(release.changelog_text.strip())}" if release.changelog_text else None
    return card(
        head(top, release.project_thumbnail_url),
        details,
        discord.ui.Separator() if changelog else None,
        changelog,
        f"-# Project #{release.project_id} · Source: {release.source_name}",
        color=CURSEFORGE_COLOR,
        buttons=_build_release_buttons(release),
    )


def _build_release_buttons(release: CurseForgeRelease) -> list[discord.ui.Button]:
    buttons = [discord.ui.Button(label="Open on CurseForge", url=release.file_page_url)]
    if release.file_download_url and release.file_download_url != release.file_page_url:
        buttons.append(discord.ui.Button(label="Direct Download", url=release.file_download_url))
    return buttons


class CurseForgeUpdatesCog(ReloadableCog):
    """Announces new DragonMineZ CurseForge releases."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.client = CurseForgeClient(self.settings)
        self._poll_lock = asyncio.Lock()

    @property
    def settings(self):
        return self.__dict__.get("_settings_override") or self.bot.settings

    @settings.setter
    def settings(self, value) -> None:
        # Tests build the cog without a bot and pin settings directly.
        self.__dict__["_settings_override"] = value

    def _start_polling_if_configured(self) -> None:
        if self.poll_curseforge.is_running():
            return
        if not self.settings.curseforge_enabled:
            logger.info("CurseForge updates disabled in settings.")
            return
        if self.settings.curseforge_announcement_channel_id is None:
            logger.warning("CurseForge announcement channel is not configured; updater will stay disabled.")
            return

        self.poll_curseforge.change_interval(
            minutes=max(self.settings.curseforge_poll_minutes, 1),
        )
        self.poll_curseforge.start()
        logger.info(
            "CurseForge polling loop started for project %s every %s minutes.",
            self.settings.curseforge_project_id,
            self.settings.curseforge_poll_minutes,
        )

    async def on_startup(self) -> None:
        self._start_polling_if_configured()

    async def on_shutdown(self) -> None:
        self.poll_curseforge.cancel()

    @tasks.loop(minutes=15)
    async def poll_curseforge(self) -> None:
        async with self._poll_lock:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("CurseForge polling failed")

    @poll_curseforge.before_loop
    async def _before_poll(self) -> None:
        await self.bot.wait_until_ready()

    async def _poll_once(self) -> None:
        if self.client._settings is not self.settings:
            self.client = CurseForgeClient(self.settings)
        release = await self.client.fetch_latest_release()
        state = await get_curseforge_project_state(release.project_id)

        if state is None or state.last_processed_file_id is None:
            await upsert_curseforge_project_state(release)
            logger.info(
                "Seeded CurseForge updater with file %s (%s) without announcing.",
                release.file_id,
                release.file_display_name,
            )
            return

        if state.last_processed_file_id == release.file_id:
            return

        channel = await self._resolve_target_channel()
        if channel is None:
            logger.error(
                "CurseForge announcement channel %s could not be resolved.",
                self.settings.curseforge_announcement_channel_id,
            )
            return

        await channel.send(
            view=_build_release_card(release),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await upsert_curseforge_project_state(release)
        logger.info(
            "Announced DragonMineZ CurseForge update file=%s previous_file=%s",
            release.file_id,
            state.last_processed_file_id,
        )

    async def _resolve_target_channel(self) -> discord.abc.Messageable | None:
        channel_id = self.settings.curseforge_announcement_channel_id
        if channel_id is None:
            return None

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception:
                logger.exception("Failed to fetch CurseForge announcement channel %s", channel_id)
                return None

        return channel if hasattr(channel, "send") else None


def setup(bot: discord.Bot) -> None:
    bot.add_cog(CurseForgeUpdatesCog(bot))
