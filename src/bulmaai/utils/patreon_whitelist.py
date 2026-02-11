# src/bulmaai/utils/patreon_whitelist.py
from __future__ import annotations

from typing import Any, Dict, Optional, TYPE_CHECKING

import discord

from bulmaai.cogs.admin import AdminCog

if TYPE_CHECKING:
    from bulmaai.bot import BulmaAI


async def start_patreon_whitelist_flow(
    discord_user_id: str,
    ticket_channel_id: str,
    _bot_context: Optional["BulmaAI"] = None,  # Injected by caller
) -> Dict[str, Any]:
    """
    Tool implementation for 'start_patreon_whitelist_flow'.

    This:
    - Resolves the Discord member and channel.
    - Finds AdminCog.
    - Calls AdminCog.start_whitelist_flow_for_user(member, channel).
    - Returns a JSON summary for the model.

    NOTE: The tool is meant to be called by the OpenAI model, not directly.
    The _bot_context parameter is injected by the openai_client, not by the LLM.
    """

    if _bot_context is None:
        return {
            "status": "error",
            "reason": "Bot context not provided to tool function.",
        }

    bot: discord.Bot = _bot_context

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


    status_text = await admin_cog.start_whitelist_flow_for_user(member, channel)

    return {
        "status": "ok",
        "message": status_text,
        "user_id": discord_user_id,
        "channel_id": ticket_channel_id,
    }
