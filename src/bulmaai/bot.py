import logging
from typing import Any

import discord
from dotenv import load_dotenv

from .config import Settings, load_settings
from .logging_setup import setup_logging
from .database.db import init_db_pool, close_db_pool

log = logging.getLogger("bulmaai")


class BulmaAI(discord.Bot):
    """Main bot class for BulmaAI."""

    instance: "BulmaAI | None" = None

    def __init__(self, settings: Settings):
        intents = discord.Intents.all()

        # debug_guilds for testing, remove when ready for production. This makes command registration much faster.
        debug_guilds = [settings.dev_guild_id] if settings.dev_guild_id else None

        super().__init__(
            intents=intents,
            debug_guilds=debug_guilds,
            auto_sync_commands=True,  # Default is True, but being explicit is nice.
        )

        self.settings = settings
        # Set instance immediately so tools can access it
        BulmaAI.instance = self
        log.info(f"🟢 BulmaAI.instance set in __init__ as {BulmaAI.instance} (id={id(BulmaAI.instance)})")


    async def setup_hook(self) -> None:
        """Called when the bot is starting up, before connecting to Discord."""
        log.info("Initializing database pool...")
        await init_db_pool()
        log.info("Database pool initialized")

    async def login(self, *args: Any, **kwargs: Any) -> Any:
        res = await super().login(*args, **kwargs)
        await self.setup_hook()
        log.info("✅ setup_hook() completed")
        return res

    def load_pr_extensions(self) -> None:
        log.info("🔵 load_pr_extensions START")
        for ext in self.settings.initial_extensions:
            try:
                log.info(f"  Loading extension: {ext}")
                self.load_extension(ext)
                log.info(f"  ✅ Loaded extension: {ext}")
            except Exception:
                log.exception(f"  ❌ Failed to load extension: {ext}")

    async def on_ready(self) -> None:
        log.info("🟢 on_ready called")
        log.info("Logged in as %s (id=%s)", self.user, getattr(self.user, "id", None))
        log.info("Guilds: %d", len(self.guilds))

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


def get_bot_instance() -> BulmaAI:
    if BulmaAI.instance is None:
        raise RuntimeError("BulmaAI instance not initialized yet.")
    return BulmaAI.instance


def run() -> None:
    load_dotenv()
    settings = load_settings()
    setup_logging(settings.log_level)

    log.info("🔵 Creating BulmaAI instance...")
    bot = BulmaAI(settings)

    log.info("🔵 Loading extensions...")
    bot.load_pr_extensions()

    log.info(f"🔵 Starting bot.run() with token. BulmaAI.instance={BulmaAI.instance}")
    bot.run(settings.discord_token)
