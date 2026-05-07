# src/bulmaai/utils/patreon_whitelist.py
from __future__ import annotations
import logging
from typing import Any, Dict, Optional, TYPE_CHECKING

import discord

from bulmaai.cogs.patreon_whitelist_flow import PatreonWhitelistFlowCog

if TYPE_CHECKING:
    from bulmaai.bot import BulmaAI

log = logging.getLogger(__name__)


async def start_patreon_whitelist_flow(
        discord_user_id: str,
        ticket_channel_id: str,
        _bot_context: Optional["BulmaAI"] = None,  # Injected by caller
        nickname: str | None = None,
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
    channel: discord.abc.Messageable | None = None
    channel_id = int(ticket_channel_id)
    user_id = int(discord_user_id)

    # Resolve guild text channels first. DM whitelist requests use a DM channel,
    # so fall back to the bot/channel API if no guild text channel matches.
    for g in bot.guilds:
        c = g.get_channel(channel_id)
        if isinstance(c, discord.TextChannel):
            guild = g
            channel = c
            log.info("Found ticket channel %s in guild %s", c.id, g.id)
            break

    if channel is None:
        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except Exception:
                channel = None
                log.warning("Failed to fetch channel %s", channel_id)

    if channel is None or not hasattr(channel, "send"):
        return {
            "status": "error",
            "reason": "Request channel not found or is not messageable.",
        }

    # Try to get member from cache first, then fetch from API if not cached
    if guild is not None:
        member = guild.get_member(user_id)
    else:
        for candidate_guild in bot.guilds:
            member = candidate_guild.get_member(user_id)
            if member is not None:
                guild = candidate_guild
                break

    if member is None:
        for candidate_guild in bot.guilds:
            try:
                member = await candidate_guild.fetch_member(user_id)
                break
            except discord.NotFound:
                continue
            except Exception as e:
                log.error("Error fetching member %s from guild %s: %s", discord_user_id, candidate_guild.id, e)
        if member is None:
            return {
                "status": "error",
                "reason": "Discord member not found in the DragonMine Z Server. They may not be in the server.",
            }

    # Get Patreon whitelist cog
    whitelist_cog = bot.get_cog("PatreonWhitelistFlowCog") or bot.get_cog("AiOnMessage")
    if not isinstance(whitelist_cog, PatreonWhitelistFlowCog):
        return {
            "status": "error",
            "reason": "PatreonWhitelistFlowCog not loaded; cannot start whitelist flow.",
        }

    status_text = await whitelist_cog.start_whitelist_flow_for_user(member, channel, nickname)

    return {
        "status": "ok",
        "message": status_text,
        "user_id": discord_user_id,
        "channel_id": ticket_channel_id,
        "mc_nickname_used": nickname,
        "user_message_sent": True,
        "suppress_ai_reply": True,
    }
