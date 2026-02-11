# src/bulmaai/utils/patreon_whitelist.py
from __future__ import annotations
import logging
from typing import Any, Dict, Optional, TYPE_CHECKING

import discord

from bulmaai.cogs.admin import AdminCog

if TYPE_CHECKING:
    from bulmaai.bot import BulmaAI

log = logging.getLogger(__name__)


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
            log.info("Found ticket channel %s in guild %s", c.id, g.id)
            break

    if guild is None or channel is None:
        return {
            "status": "error",
            "reason": "Ticket channel not found in any guild.",
        }

    # Try to get member from cache first, then fetch from API if not cached
    member = guild.get_member(int(discord_user_id))
    if member is None:
        log.info("Member %s not in cache, fetching from Discord API...", discord_user_id)
        try:
            member = await guild.fetch_member(int(discord_user_id))
            log.info("Successfully fetched member %s from API", discord_user_id)
        except discord.NotFound:
            log.error("Member %s not found in guild %s (not in server)", discord_user_id, guild.id)
            return {
                "status": "error",
                "reason": "Discord member not found in the guild. They may not be in the server.",
            }
        except Exception as e:
            log.error("Error fetching member %s: %s", discord_user_id, e)
            return {
                "status": "error",
                "reason": f"Error fetching member from Discord API: {e}",
            }
    else:
        log.info("Found member %s in cache for guild %s", discord_user_id, guild.id)

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
