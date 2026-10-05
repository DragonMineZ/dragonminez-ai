import asyncio
import logging
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import discord
from dotenv import load_dotenv

from .config import Settings, load_settings
from .database.db import close_db_pool, init_db_pool
from .logging_setup import setup_logging
from .services.discord_log_forwarding import (
    DiscordLogForwarder,
    install_discord_log_forwarder,
)
from .services import ai_budget, db_schema, message_presets, support_traces
from .services.release_webhook import ReleaseWebhookServer
from .utils import permissions
from .utils.lifecycle import lifecycle_cogs
from .web import server as panel_server

log = logging.getLogger("bulmaai")

UNGATED_EVENTS = frozenset({"interaction", "guild_join", "guild_remove"})


def event_guild_id(arg: object) -> int | None:
    """The server an event argument belongs to (Guild, message/member/channel, or raw payload), else None."""
    if isinstance(arg, discord.Guild):
        return arg.id
    guild = getattr(arg, "guild", None)
    if isinstance(guild, discord.Guild):
        return guild.id
    guild_id = getattr(arg, "guild_id", None)
    return guild_id if isinstance(guild_id, int) else None

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTART_EMBED_COLOR = discord.Colour.from_rgb(46, 204, 113)
# Every send merges over this: the bot pings nobody unless that call names the exact users/roles
# (only the person it is answering, or what an admin picked). Without it, AllowedMentions(users=True)
# or no allowed_mentions at all let AI text and names like "@everyone" ping the whole server.
SAFE_MENTIONS = discord.AllowedMentions(everyone=False, roles=False, users=False, replied_user=True)

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
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True

        debug_guilds = [settings.dev_guild_id] if settings.dev_guild_id else None

        super().__init__(
            intents=intents,
            debug_guilds=debug_guilds,
            auto_sync_commands=True,
            # Every command assumes a guild Member; hide them in DMs instead of crashing there.
            default_command_contexts={discord.InteractionContextType.guild},
            allowed_mentions=SAFE_MENTIONS,
        )

        self.settings = settings
        self._restart_announcement_sent = False
        self.restart_requested = False
        self._discord_log_forwarder: DiscordLogForwarder | None = None
        # Cog state parked across a hot reload (see utils/lifecycle.py) and the lock every update takes.
        self.reload_state: dict[str, object] = {}
        self.update_lock = asyncio.Lock()
        self.update_task: asyncio.Task | None = None
        self.panel_server = None
        self.release_webhook_server: ReleaseWebhookServer | None = None
        self._servers_started = False
        BulmaAI.instance = self

    async def start(self, token: str, *, reconnect: bool = True) -> None:
        # py-cord has no discord.py-style setup_hook; run() goes through start(), so hook in here.
        await self.setup_hook()
        await super().start(token, reconnect=reconnect)

    async def setup_hook(self) -> None:
        """Called when the bot is starting up, before connecting to Discord."""
        # Forwarder first so a failing DB/schema step below still reaches the log channel.
        if self.settings.discord_log_forwarding_enabled and self.settings.discord_log_channel_id:
            self._discord_log_forwarder = install_discord_log_forwarder(
                bot=self,
                channel_id=self.settings.discord_log_channel_id,
                min_level_name=self.settings.discord_log_min_level,
            )
        try:
            await init_db_pool()
            await db_schema.ensure_schema()
        except Exception:
            # Non-fatal: get_pool() retries lazily, same as before this hook actually ran.
            log.exception("Database setup failed (pool or scripts/schema.sql); continuing")
        try:
            midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            ai_budget.seed(await support_traces.sum_tokens_by_model_since(midnight))
        except Exception:
            log.exception("Failed to seed today's OpenAI token budget from traces")
        message_presets.ensure_message_presets_file()

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
        for guild in self.guilds:
            if not permissions.is_allowed_guild_id(guild.id, self.settings):
                log.info("Bot is in other server %s (%s); ignoring everything there", guild.name, guild.id)
        if not self._servers_started:
            self._servers_started = True
            await self.start_panel()
            self.start_release_webhook()
        await self.start_cog_lifecycles()
        if self._restart_announcement_sent:
            return

        if await self._send_restart_announcement():
            self._restart_announcement_sent = True

    async def start_cog_lifecycles(self) -> None:
        async def start(cog) -> None:
            try:
                await cog.start_lifecycle()
            except Exception:
                log.exception("%s failed to start", cog.qualified_name)

        await asyncio.gather(*(start(cog) for cog in lifecycle_cogs(self)))

    # The panel and the release webhook server live on the bot, not in a cog, so reloading any cog
    # (including the ones that register webhook routes) never takes them down.
    async def start_panel(self) -> None:
        settings = self.settings
        if self.panel_server is not None or not settings.panel_enabled:
            return
        if not settings.panel_session_secret or not settings.discord_oauth_client_secret:
            log.error("Admin panel needs PANEL_SESSION_SECRET and DISCORD_OAUTH_CLIENT_SECRET; not starting.")
            return
        server = panel_server.PanelServer(self)
        try:
            await server.start()
        except OSError:
            log.exception("Admin panel failed to bind %s:%s", settings.panel_host, settings.panel_port)
            return
        self.panel_server = server

    async def stop_panel(self) -> None:
        if self.panel_server is not None:
            await self.panel_server.stop()
            self.panel_server = None

    def start_release_webhook(self) -> None:
        settings = self.settings
        if self.release_webhook_server is not None or not settings.release_webhook_enabled:
            return
        if not settings.release_webhook_secret:
            log.error(
                "Release webhook is enabled but DMZ_RELEASE_BOT_WEBHOOK_SECRET is not set; "
                "webhook listener will not start."
            )
            return
        server = ReleaseWebhookServer(
            host=settings.release_webhook_host,
            port=settings.release_webhook_port,
            path=settings.release_webhook_path,
            secret=settings.release_webhook_secret,
            loop=asyncio.get_running_loop(),
            on_payload=self._handle_release_payload,
        )
        try:
            server.start()
        except OSError:
            log.exception("Release webhook failed to bind %s:%s", settings.release_webhook_host, settings.release_webhook_port)
            return
        self.release_webhook_server = server

    async def _handle_release_payload(self, payload: dict) -> None:
        cog = self.get_cog("ReleaseApprovalCog")
        if cog is None:
            raise RuntimeError("Release candidate arrived while ReleaseApprovalCog isn't loaded")
        await cog.handle_webhook_payload(payload)

    async def close(self) -> None:
        """Called when the bot is shutting down."""
        await asyncio.gather(
            *(cog.stop_lifecycle() for cog in lifecycle_cogs(self)), return_exceptions=True
        )
        await self.stop_panel()
        if self.release_webhook_server is not None:
            await asyncio.to_thread(self.release_webhook_server.stop)
            self.release_webhook_server = None
        if self._discord_log_forwarder is not None:
            await self._discord_log_forwarder.stop()
            self._discord_log_forwarder = None
        log.info("Closing database pool...")
        await close_db_pool()
        log.info("Database pool closed")
        await super().close()

    async def invoke_application_command(self, ctx: discord.ApplicationContext) -> None:
        # Public-bot gate: every command is global, so refuse anything run outside our servers.
        if not permissions.is_allowed_guild_id(ctx.guild_id, self.settings):
            await ctx.respond("This bot only works in the official DragonMineZ server.", ephemeral=True)
            return
        await super().invoke_application_command(ctx)

    def dispatch(self, event: str, *args, **kwargs) -> None:
        # Public-bot gate for events: the bot may sit in other servers but does nothing there (no automod,
        # XP, welcome, log parsing...). Interactions still pass so commands get the refusal above; DMs pass.
        if event not in UNGATED_EVENTS and any(
            not permissions.is_allowed_guild_id(guild_id, self.settings) for guild_id in map(event_guild_id, args) if guild_id
        ):
            return
        super().dispatch(event, *args, **kwargs)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if not permissions.is_allowed_guild_id(guild.id, self.settings):
            log.info("Added to other server %s (%s); staying but ignoring it", guild.name, guild.id)

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
    if bot.restart_requested:
        # Non-zero so systemd restarts us under Restart=on-failure as well as Restart=always.
        sys.exit(1)
