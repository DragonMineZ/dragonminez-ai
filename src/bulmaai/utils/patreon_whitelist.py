# src/bulmaai/utils/patreon_whitelist.py
from __future__ import annotations

from typing import Any, Dict

import discord

from bulmaai.cogs.admin import AdminCog


async def start_patreon_whitelist_flow(
    discord_user_id: str,
    ticket_channel_id: str,
) -> Dict[str, Any]:
    """
    Tool implementation for 'start_patreon_whitelist_flow'.

    This:
    - Resolves the Discord member and channel.
    - Finds AdminCog.
    - Calls AdminCog.start_whitelist_flow_for_user(member, channel).
    - Returns a JSON summary for the model.

    NOTE: The tool is meant to be called by the OpenAI model, not directly.
    """

    # Import inside function to avoid circular import at module load time
    from bulmaai.utils import tools_registry

    # Get the bot instance from tools_registry
    bot: discord.Bot = tools_registry.get_bot_instance()

    guild = None
    member: discord.Member | None = None
    channel: discord.TextChannel | None = None

    # Resolve channel first (we only have IDs; assume single-main guild)
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
        }

    member = guild.get_member(int(discord_user_id))
    if member is None:
        return {
            "status": "error",
            "reason": "Discord member not found in the guild.",
        }

    # Get AdminCog
    admin_cog = bot.get_cog("AdminCog")
    if not isinstance(admin_cog, AdminCog):
        return {
            "status": "error",
            "reason": "AdminCog not loaded; cannot start whitelist flow.",
        }

    # Optional: only allow if the user has Patreon roles; you can reuse your existing
    # Patreon role check here, or trust that the model only calls this when appropriate.
    # Example:
    # from bulmaai.cogs.ai_tickets import PATREON_ROLE_IDS
    # if not any(r.id in PATREON_ROLE_IDS for r in member.roles):
    #     return {
    #         "status": "error",
    #         "reason": "User does not have a Patreon role; whitelist flow not started.",
    #     }

    status_text = await admin_cog.start_whitelist_flow_for_user(member, channel)

    return {
        "status": "ok",
        "message": status_text,
        "user_id": discord_user_id,
        "channel_id": ticket_channel_id,
    }
