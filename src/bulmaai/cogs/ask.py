import logging
import math
import time
from collections import defaultdict
from collections.abc import Sequence

import discord
from discord.ext import commands

from bulmaai.cogs.ai_tickets import _chunk_discord_message
from bulmaai.services.ai_tools import SUPPORT_TOOL_NAMES
from bulmaai.services.moderation import ModerationState
from bulmaai.services.openai_client import (
    ConversationMessage,
    is_transient_ai_error,
    run_support_agent,
)
from bulmaai.utils.permissions import is_staff

log = logging.getLogger(__name__)


def is_ask_channel_allowed(channel_id: int, allowed_channel_ids: Sequence[int]) -> bool:
    return channel_id in set(allowed_channel_ids)


def evaluate_ask_rate_limit(
    events: Sequence[float],
    *,
    now: float,
    window_seconds: float,
    max_calls: int,
) -> float | None:
    """Return None if the call is allowed, else the seconds until the next one is."""
    if len(events) <= max_calls:
        return None
    return max(0.0, min(events) + window_seconds - now)


class AskCog(commands.Cog):
    """Public `/ask` entry point into the wiki-RAG support agent."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._rate_limit_state = ModerationState()
        self._rate_limit_events: dict[tuple[int, int], list[float]] = defaultdict(list)

    @discord.slash_command(name="ask", description="Ask the DragonMineZ AI support agent a question.")
    @discord.option("question", description="Your DragonMineZ question", required=True)
    @discord.option(
        "public",
        description="Share the answer with the channel instead of just you",
        required=False,
        default=False,
    )
    async def ask(self, ctx: discord.ApplicationContext, question: str, public: bool = False) -> None:
        settings = self.bot.settings
        channel_id = ctx.channel.id

        if not is_ask_channel_allowed(channel_id, settings.ask_allowed_channel_ids):
            await ctx.respond("`/ask` isn't enabled in this channel.", ephemeral=True)
            return

        now = time.monotonic()
        events = self._rate_limit_state.record(
            self._rate_limit_events,
            (ctx.author.id, 0),
            now,
            settings.ask_rate_limit_window_seconds,
        )
        retry_after = evaluate_ask_rate_limit(
            events,
            now=now,
            window_seconds=settings.ask_rate_limit_window_seconds,
            max_calls=settings.ask_rate_limit_max_calls,
        )
        if retry_after is not None:
            await ctx.respond(
                f"You're asking too fast. Try again in {math.ceil(retry_after)}s.",
                ephemeral=True,
            )
            return

        await ctx.defer(ephemeral=not public)

        messages: list[ConversationMessage] = [
            ConversationMessage(
                role="user",
                content=question,
                speaker_name=getattr(ctx.author, "display_name", ctx.author.name),
                speaker_id=str(ctx.author.id),
                speaker_kind="requester",
            )
        ]

        try:
            result = await run_support_agent(
                messages=messages,
                enabled_tools=SUPPORT_TOOL_NAMES,
                language_hint=None,
                user_id=ctx.author.id,
                channel_id=channel_id,
                ticket_conversation=False,
                bot=self.bot,
                settings=settings,
                context_lines=[
                    f"channel: /ask slash command in #{getattr(ctx.channel, 'name', '?')} (single question, no history)",
                    f"requester: {getattr(ctx.author, 'display_name', ctx.author.name)}",
                ],
                requester_is_staff=isinstance(ctx.author, discord.Member) and is_staff(ctx.author, settings=settings),
                channel_kind="public",
            )
        except Exception as error:
            transient = is_transient_ai_error(error)
            log.exception(
                "/ask error: %s",
                error,
                extra={
                    "event": "ask_command_error",
                    "guild_id": getattr(ctx.guild, "id", None),
                    "channel_id": channel_id,
                    "user_id": ctx.author.id,
                    "transient": transient,
                },
            )
            await ctx.followup.send(
                "I ran into an error answering that. Please try again in a moment.",
                ephemeral=True,
            )
            return

        if result.get("paused"):
            await ctx.followup.send(
                "My AI circuits are recharging for today ⚡ Try again after 00:00 UTC.",
                ephemeral=True,
            )
            return

        reply_text = result["reply"].strip()
        if not reply_text or reply_text == "(no reply)":
            reply_text = "I couldn't find a confident knowledge-backed answer for that."

        for chunk in _chunk_discord_message(reply_text):
            await ctx.followup.send(chunk, ephemeral=not public)


def setup(bot: discord.Bot):
    bot.add_cog(AskCog(bot))
