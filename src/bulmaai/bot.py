import asyncio
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import discord
from dotenv import load_dotenv

from .config import Settings, load_settings
from .database.db import close_db_pool, init_db_pool
from .logging_setup import setup_logging
from .services.docs_ingestion import ensure_schema
from .services.message_presets import ensure_message_presets_file

log = logging.getLogger("bulmaai")

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTART_EMBED_COLOR = discord.Colour.from_rgb(46, 204, 113)


@dataclass(frozen=True)
class GitRuntimeInfo:
    branch: str
    commit_sha: str
    short_sha: str
    subject: str
    committed_at: datetime | None
    repo_url: str | None
    commit_url: str | None
    dirty: bool


def _run_git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _normalize_github_remote(remote_url: str) -> str | None:
    value = remote_url.strip()
    if not value:
        return None

    if value.startswith("git@github.com:"):
        value = "https://github.com/" + value.removeprefix("git@github.com:")
    elif value.startswith("ssh://git@github.com/"):
        value = "https://github.com/" + value.removeprefix("ssh://git@github.com/")
    elif not value.startswith("https://github.com/"):
        return None

    if value.endswith(".git"):
        value = value[:-4]

    return value.rstrip("/")


def _load_git_runtime_info() -> GitRuntimeInfo | None:
    try:
        branch = _run_git("rev-parse", "--abbrev-ref", "HEAD")
        commit_sha = _run_git("rev-parse", "HEAD")
        short_sha = _run_git("rev-parse", "--short", "HEAD")
        subject = _run_git("log", "-1", "--pretty=%s")
        committed_at_raw = _run_git("log", "-1", "--pretty=%cI")
        remote_url = _run_git("remote", "get-url", "origin")
        dirty = bool(_run_git("status", "--porcelain"))
    except (FileNotFoundError, subprocess.CalledProcessError):
        log.exception("Failed to load runtime git metadata")
        return None

    committed_at: datetime | None = None
    if committed_at_raw:
        try:
            committed_at = datetime.fromisoformat(committed_at_raw)
        except ValueError:
            committed_at = None

    repo_url = _normalize_github_remote(remote_url)
    commit_url = f"{repo_url}/commit/{commit_sha}" if repo_url else None

    return GitRuntimeInfo(
        branch=branch,
        commit_sha=commit_sha,
        short_sha=short_sha,
        subject=subject,
        committed_at=committed_at,
        repo_url=repo_url,
        commit_url=commit_url,
        dirty=dirty,
    )


class BulmaAI(discord.Bot):
    """Main bot class for BulmaAI."""

    instance: "BulmaAI | None" = None

    def __init__(self, settings: Settings):
        intents = discord.Intents.all()

        debug_guilds = [settings.dev_guild_id] if settings.dev_guild_id else None

        super().__init__(
            intents=intents,
            debug_guilds=debug_guilds,
            auto_sync_commands=True,  # Default is True, but being explicit is nice.
        )

        self.settings = settings
        self._restart_announcement_sent = False
        BulmaAI.instance = self

    async def setup_hook(self) -> None:
        """Called when the bot is starting up, before connecting to Discord."""
        await init_db_pool()
        await ensure_schema()
        ensure_message_presets_file()

    def reload_settings(self) -> Settings:
        self.settings = load_settings()
        return self.settings

    def load_pr_extensions(self) -> None:
        for ext in self.settings.initial_extensions:
            try:
                log.info(f"  Loading extension: {ext}")
                self.load_extension(ext)
                log.info(f"  Loaded extension: {ext}")
            except Exception:
                log.exception(f"  Failed to load extension: {ext}")

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s)", self.user, getattr(self.user, "id", None))
        if self._restart_announcement_sent:
            return

        if await self._send_restart_announcement():
            self._restart_announcement_sent = True

    async def close(self) -> None:
        """Called when the bot is shutting down."""
        log.info("Closing database pool...")
        await close_db_pool()
        log.info("Database pool closed")
        await super().close()

    async def on_application_command_error(
            self,
            ctx: discord.ApplicationContext,
            error: Exception,
    ) -> None:
        log.exception("Application command error: %s", error)
        if ctx.response.is_done():
            await ctx.followup.send("Something went wrong.", ephemeral=True)
        else:
            await ctx.respond("Something went wrong.", ephemeral=True)

    async def _send_restart_announcement(self) -> bool:
        channel_id = self.settings.bot_restart_channel_id
        if channel_id is None:
            log.warning("BOT_RESTART_CHANNEL_ID is missing; restart announcement skipped.")
            return True

        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except Exception:
                log.exception("Failed to fetch restart announcement channel %s", channel_id)
                return False

        if not hasattr(channel, "send"):
            log.error("Configured restart announcement channel %s is not messageable.", channel_id)
            return False

        embed, view = await self._build_restart_announcement()

        try:
            await channel.send(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            log.exception("Failed to send restart announcement to channel %s", channel_id)
            return False

        return True

    async def _build_restart_announcement(self) -> tuple[discord.Embed, discord.ui.View | None]:
        git_info = await asyncio.to_thread(_load_git_runtime_info)
        now = datetime.now(timezone.utc)
        user_name = getattr(self.user, "display_name", "BulmaAI")

        embed = discord.Embed(
            title="Bot Restarted Successfully",
            description="BulmaAI is back online and ready to serve.",
            colour=RESTART_EMBED_COLOR,
            timestamp=now,
        )
        embed.set_author(name=user_name)
        if self.user is not None:
            embed.set_thumbnail(url=self.user.display_avatar.url)

        embed.add_field(
            name="Restart Time",
            value=f"<t:{int(now.timestamp())}:F>\n<t:{int(now.timestamp())}:R>",
            inline=True,
        )
        embed.add_field(name="Status", value="Connected to Discord", inline=True)

        view: discord.ui.View | None = None
        if git_info is None:
            embed.add_field(
                name="GitHub Reference",
                value="Unavailable for this runtime.",
                inline=False,
            )
            embed.set_footer(text="Runtime source metadata unavailable")
            return embed, None

        tree_state = "Dirty" if git_info.dirty else "Clean"
        embed.add_field(name="Branch", value=f"`{git_info.branch}`", inline=True)
        embed.add_field(
            name="Running Commit",
            value=f"`{git_info.short_sha}`\n{git_info.subject}",
            inline=True,
        )
        embed.add_field(name="Working Tree", value=tree_state, inline=True)

        if git_info.committed_at is not None:
            embed.add_field(
                name="GitHub Reference",
                value=(
                    f"Commit `{git_info.short_sha}`\n"
                    f"<t:{int(git_info.committed_at.timestamp())}:F>\n"
                    f"<t:{int(git_info.committed_at.timestamp())}:R>"
                ),
                inline=False,
            )
        else:
            embed.add_field(
                name="GitHub Reference",
                value=f"Commit `{git_info.short_sha}`",
                inline=False,
            )

        embed.set_footer(text=f"Repo branch: {git_info.branch}")

        if git_info.commit_url or git_info.repo_url:
            view = discord.ui.View()
            if git_info.commit_url:
                view.add_item(
                    discord.ui.Button(label="View Running Commit", url=git_info.commit_url)
                )
            if git_info.repo_url:
                view.add_item(
                    discord.ui.Button(label="Open Repository", url=git_info.repo_url)
                )

        return embed, view


def get_bot_instance() -> BulmaAI:
    if BulmaAI.instance is None:
        raise RuntimeError("BulmaAI instance not initialized yet.")
    return BulmaAI.instance


def run() -> None:
    load_dotenv()
    settings = load_settings()
    setup_logging(settings.log_level)

    bot = BulmaAI(settings)

    bot.load_pr_extensions()

    bot.run(settings.discord_token)
