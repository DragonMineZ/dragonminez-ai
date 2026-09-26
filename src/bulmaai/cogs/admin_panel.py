import logging

import discord
from discord.ext import commands

from bulmaai.web.server import PanelServer


log = logging.getLogger(__name__)


class AdminPanelCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.server: PanelServer | None = None

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self.server is not None:
            return
        settings = self.bot.settings
        if not settings.panel_enabled:
            return
        if not settings.panel_session_secret or not settings.discord_oauth_client_secret:
            log.error("Admin panel needs PANEL_SESSION_SECRET and DISCORD_OAUTH_CLIENT_SECRET; not starting.")
            return
        self.server = PanelServer(self.bot)
        try:
            await self.server.start()
        except OSError:
            log.exception("Admin panel failed to bind %s:%s", settings.panel_host, settings.panel_port)
            self.server = None

    def cog_unload(self) -> None:
        if self.server is not None:
            self.bot.loop.create_task(self.server.stop())
            self.server = None


def setup(bot: discord.Bot):
    bot.add_cog(AdminPanelCog(bot))
