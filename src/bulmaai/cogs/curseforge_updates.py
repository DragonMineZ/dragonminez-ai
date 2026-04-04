import asyncio
import logging
import math

import discord
from discord.ext import commands, tasks

from bulmaai.services.curseforge_client import CurseForgeClient, CurseForgeRelease
from bulmaai.services.curseforge_state import (
    get_curseforge_project_state,
    upsert_curseforge_project_state,
)

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


def _build_release_embed(release: CurseForgeRelease) -> discord.Embed:
    description_lines: list[str] = []
    if release.project_summary:
        description_lines.append(release.project_summary.strip())

    if release.changelog_text:
        description_lines.append(f"**Changelog**\n{_truncate(release.changelog_text.strip())}")

    embed = discord.Embed(
        title=release.file_display_name,
        url=release.file_page_url,
        description="\n\n".join(description_lines) or "A new DragonMineZ file is available on CurseForge.",
        color=CURSEFORGE_COLOR,
        timestamp=release.uploaded_at,
    )
    embed.set_author(
        name=f"{release.project_title} on CurseForge",
        url=release.project_url,
    )

    if release.project_thumbnail_url:
        embed.set_thumbnail(url=release.project_thumbnail_url)

    embed.add_field(name="Release Type", value=_humanize_release_type(release.release_type), inline=True)
    embed.add_field(
        name="Uploaded",
        value=f"<t:{int(release.uploaded_at.timestamp())}:F>\n<t:{int(release.uploaded_at.timestamp())}:R>",
        inline=True,
    )
    embed.add_field(name="File Size", value=_format_bytes(release.file_size_bytes), inline=True)

    if release.minecraft_versions:
        embed.add_field(name="Minecraft", value=", ".join(release.minecraft_versions[:6]), inline=True)
    if release.loader_tags:
        embed.add_field(name="Loaders", value=", ".join(release.loader_tags[:6]), inline=True)
    if release.environment_tags:
        embed.add_field(name="Tags", value=", ".join(release.environment_tags[:6]), inline=True)
    if release.download_count is not None:
        embed.add_field(name="Downloads", value=f"{release.download_count:,}", inline=True)

    embed.add_field(name="File", value=f"`{release.file_name}`", inline=False)
    embed.set_footer(text=f"Project #{release.project_id} | Source: {release.source_name}")
    return embed


def _build_release_view(release: CurseForgeRelease) -> discord.ui.View:
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Open on CurseForge", url=release.file_page_url))
    if release.file_download_url and release.file_download_url != release.file_page_url:
        view.add_item(discord.ui.Button(label="Direct Download", url=release.file_download_url))
    return view


class CurseForgeUpdatesCog(commands.Cog):
    """Announces new DragonMineZ CurseForge releases."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.settings = bot.settings
        self.client = CurseForgeClient(self.settings)
        self._poll_lock = asyncio.Lock()

    async def cog_load(self) -> None:
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

    def cog_unload(self) -> None:
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
            embed=_build_release_embed(release),
            view=_build_release_view(release),
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
