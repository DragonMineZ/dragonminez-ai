import logging
import discord

from discord.ext import commands

from bulmaai.services.openai_client import run_support_agent

log = logging.getLogger(__name__)

# Ticket category ID you gave
TICKETS_CATEGORY_ID = 1262517992982315110

# Patreon roles (fill with actual IDs)
PATREON_ROLE_IDS = {
    1287877272224665640,
    1287877305259130900,
}


def _user_has_patreon_role(member: discord.Member) -> bool:
    return any(role.id in PATREON_ROLE_IDS for role in member.roles)


def _is_ticket_channel(channel: discord.abc.GuildChannel) -> bool:
    return (
        isinstance(channel, discord.TextChannel)
        and channel.category
        and channel.category.id == TICKETS_CATEGORY_ID
    )


class AITicketsCog(commands.Cog):
    """AI triage / support for ticket channels."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    async def _build_history(self, channel: discord.TextChannel, limit: int = 10) -> list[dict[str, str]]:
        history: list[dict[str, str]] = []
        async for msg in channel.history(limit=limit, oldest_first=True):
            role = "assistant" if msg.author == self.bot.user else "user"
            history.append({"role": role, "content": msg.content})
        return history

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore bots and DMs
        if message.author.bot or not message.guild:
            return

        channel = message.channel
        if not isinstance(channel, discord.TextChannel):
            return
        if not _is_ticket_channel(channel):
            return

        # Optional: ignore messages that are clearly commands
        if message.content.startswith(("!", "/", ".")):
            return

        try:
            history = await self._build_history(channel, limit=10)
        except Exception as e:
            log.exception("Error building message history: %s", e)
            return

        enabled_tools = ["docs_search", "start_patreon_whitelist_flow"]

        try:
            result = await run_support_agent(
                messages=history,
                enabled_tools=enabled_tools,
                language_hint=None,
                user_id=message.author.id,
                channel_id=channel.id,
            )
        except Exception as e:
            log.exception("AI support error: %s", e)
            await channel.send(
                "I ran into an error while processing this. A staff member will take a look."
            )
            return

        reply_text = result["reply"]
        if reply_text:
            await channel.send(reply_text)

        if result["suggested_close"]:
            await channel.send(
                "*(AI note: I think this ticket can be closed now. A staff member should confirm.)*"
            )

        # Tool side-effects (like starting the Patreon whitelist flow)
        # are handled inside the tool functions (docs_search, start_patreon_whitelist_flow),
        # so we don't need to do anything else here.


def setup(bot: discord.Bot):
    bot.add_cog(AITicketsCog(bot))
