import logging
import math
import time
from collections import defaultdict
from collections.abc import Sequence

import discord
from discord.ext import commands

from bulmaai.cogs.ai_tickets import (
    AI_CARD_COLOR,
    CARD_TEXT_LIMIT,
    _card,
    _chunk_discord_message,
    _with_footer,
    remember_ask_question,
)
from bulmaai.services import ai_feedback as feedback_store
from bulmaai.services.ai_guard import ASK_MAX_QUESTION_LENGTH, defang_unknown_links, safe_name, screen_question
from bulmaai.services.ai_tools import LOOKUP_TOOL_NAMES
from bulmaai.services.moderation import ModerationState
from bulmaai.services.ai_client import (
    ConversationMessage,
    is_transient_ai_error,
    run_support_agent,
)
from bulmaai.ui import ai_feedback
from bulmaai.utils.permissions import is_staff

log = logging.getLogger(__name__)

REFUSAL_TEXT = (
    "I only answer DragonMineZ questions, in my own words: I won't repeat text, ping anyone or change my rules. "
    "Ask your question plainly, or open a ticket."
)


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
    @discord.option(
        "question", description="Your DragonMineZ question", required=True, max_length=ASK_MAX_QUESTION_LENGTH
    )
    async def ask(self, ctx: discord.ApplicationContext, question: str) -> None:
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

        # After the rate limit on purpose: refused attempts still burn the caller's quota.
        refusal = screen_question(question)
        if refusal is not None:
            log.warning(
                "Refused /ask from %s (%s): %r",
                ctx.author.id,
                refusal,
                question[:200],
                extra={"event": "ask_question_refused", "user_id": ctx.author.id, "channel_id": channel_id},
            )
            await ctx.respond(REFUSAL_TEXT, ephemeral=True)
            return

        await ctx.defer()

        messages: list[ConversationMessage] = [
            ConversationMessage(
                role="user",
                content=question,
                speaker_name=safe_name(getattr(ctx.author, "display_name", ctx.author.name)),
                speaker_id=str(ctx.author.id),
                speaker_kind="requester",
            )
        ]

        try:
            result = await run_support_agent(
                messages=messages,
                enabled_tools=LOOKUP_TOOL_NAMES,  # a /ask answer carries no controls
                language_hint=None,
                user_id=ctx.author.id,
                channel_id=channel_id,
                ticket_conversation=False,
                bot=self.bot,
                settings=settings,
                context_lines=[
                    f"channel: /ask slash command in #{getattr(ctx.channel, 'name', '?')} (single question, no history)",
                    f"requester: {safe_name(getattr(ctx.author, 'display_name', ctx.author.name))}",
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
        answered = bool(reply_text) and reply_text != "(no reply)"
        if not answered:
            reply_text = "I couldn't find a confident knowledge-backed answer for that."

        # Every reply is posted as written (screen_reply already blocked or defused the rest); links to pages
        # that aren't ours go out unclickable.
        if "unknown_link" in (result.get("guard_flags") or ()):
            reply_text = defang_unknown_links(reply_text, settings)
        # 👍/👎 under a real answer, as under a ping's (ui/ai_feedback); only the asker can click them.
        feedback = answered and ai_feedback.wanted(settings, result.get("kind"))
        chunks = _chunk_discord_message(reply_text, CARD_TEXT_LIMIT)
        chunks[-1] = _with_footer(chunks[-1])
        for index, chunk in enumerate(chunks):
            rows = [ai_feedback.feedback_row(ctx.author.id)] if feedback and index == len(chunks) - 1 else []
            sent = await ctx.followup.send(
                view=_card(chunk, AI_CARD_COLOR, *rows),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if index == 0:
                remember_ask_question(sent.id, ctx.author.id, messages[0]["speaker_name"], question)
            if rows:
                await self._save_feedback(sent.id, channel_id, ctx.author.id, result)

    async def _save_feedback(self, message_id: int, channel_id: int, requester_id: int, result) -> None:
        """The answer's ai_feedback row; without it a rating still counts, only the response id is lost."""
        try:
            await feedback_store.save(
                message_id=message_id,
                channel_id=channel_id,
                requester_id=requester_id,
                response_id=result.get("response_id"),
                language=result.get("language") or "en",
            )
        except Exception:
            extra = {"channel_id": channel_id, "message_id": message_id}
            log.exception("Could not save /ask answer feedback", extra=extra)


def setup(bot: discord.Bot):
    bot.add_cog(AskCog(bot))
