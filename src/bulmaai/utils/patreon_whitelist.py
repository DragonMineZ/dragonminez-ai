import logging

from typing import Any, Dict

import discord

from bulmaai.cogs.admin import AdminCog
from bulmaai.bot import BulmaAI  # your Bot subclass

log = logging.getLogger(__name__)

def get_bot_instance() -> BulmaAI:
    """
    Simple singleton-style getter.

    In BulmaAI.__init__, make sure you set:
        BulmaAI.instance = self
    """
    if getattr(BulmaAI, "instance", None) is None:
        raise RuntimeError("BulmaAI instance not initialized yet.")
    return BulmaAI.instance  # type: ignore[return-value]


async def start_patreon_whitelist_flow(
    discord_user_id: str,
    ticket_channel_id: str,
) -> Dict[str, Any]:
    """
    Tool implementation for 'start_patreon_whitelist_flow'.

    - Resolve the member and channel.
    - Find AdminCog.
    - Call AdminCog.start_whitelist_flow_for_user(member, channel).
    - Return a JSON summary that the model can use.
    """
    bot: discord.Bot = get_bot_instance()

    member: discord.Member | None = None
    channel: discord.TextChannel | None = None
    guild: discord.Guild | None = None

    # Resolve channel and guild (assumes one main guild)
    for g in bot.guilds:
        c = g.get_channel(int(ticket_channel_id))
        if isinstance(c, discord.TextChannel):
            guild = g
            channel = c
            break

    if guild is None or channel is None:
        return {
            "status": "error",
            "reason": "Ticket channel not found in any guild.",
            "user_id": discord_user_id,
            "channel_id": ticket_channel_id,
        }

    member = guild.get_member(int(discord_user_id))
    if member is None:
        log.exception(f"Member not found, instance: {bot}, guild: {guild}, user_id: {discord_user_id}")
        return {
            "status": "error",
            "reason": "Discord member not found in the guild.",
            "user_id": discord_user_id,
            "channel_id": ticket_channel_id,
        }

    admin_cog = bot.get_cog("AdminCog")
    if not isinstance(admin_cog, AdminCog):
        log.exception(f"AdminCog not found, instance: {bot}, guild: {guild}")
        return {
            "status": "error",
            "reason": "AdminCog not loaded; cannot start whitelist flow.",
            "user_id": discord_user_id,
            "channel_id": ticket_channel_id,
        }

    # Call the core workflow (no initial nickname, so it will ask for one)
    flow_status = await admin_cog.start_whitelist_flow_for_user(
        member=member,
        channel=channel,
        initial_nickname=None,
    )

    return {
        "status": "ok",
        "flow_status": flow_status,
        "user_id": discord_user_id,
        "channel_id": ticket_channel_id,
    }
