"""Staff log channels that may be forums: a forum gets one post per kind of entry (warns, bans, automod, ...),
created on first use and remembered in `log_forum_posts`; a plain text channel gets everything as before.

Kept out of the "mod*" logger names on purpose: discord_log_forwarding sends moderation warnings to this same
forum, so a failure here must never be forwarded (it would loop)."""

import asyncio
import logging

import discord

from bulmaai.database.db import get_pool
from bulmaai.ui.v2 import card

log = logging.getLogger(__name__)

ARCHIVE_MINUTES = 10080  # a week, Discord's longest; sending to an archived post reopens it anyway

# key: (emoji, name, what lands there). Order is the order staff see them described here.
MOD_LOG_POSTS: dict[str, tuple[str, str, str]] = {
    "warns": ("⚠️", "Warns", "Warns and verbal warnings, and warns staff removed."),
    "timeouts": ("🔇", "Timeouts", "Timeouts and unmutes."),
    "kicks": ("👢", "Kicks", "Kicks."),
    "bans": ("🔨", "Bans", "Bans, tempbans, softbans and unbans."),
    "automod": ("🤖", "Automod", "Automod alerts and scam image reviews."),
    "raid": ("🚪", "Raid & joins", "Raid mode and flagged joiners."),
    "commands": ("🧹", "Commands", "Purges, removed notes and other staff commands."),
    "reports": ("🚩", "Reports", "Member reports, while no reports channel is set."),
    "errors": ("🐞", "Bot errors", "Warnings and errors from the moderation code."),
}
ACTION_POSTS = {
    "warn": "warns",
    "verbal": "warns",
    "timeout": "timeouts",
    "untimeout": "timeouts",
    "kick": "kicks",
    "ban": "bans",
    "softban": "bans",
    "unban": "bans",
}

_threads: dict[tuple[int, str], int] = {}
_locks: dict[tuple[int, str], asyncio.Lock] = {}


def post_for_action(action: str) -> str:
    return ACTION_POSTS.get(action, "commands")


def title(posts: dict[str, tuple[str, str, str]], key: str) -> str:
    emoji, name, _ = posts[key]
    return f"{emoji} {name}"


async def destination(bot: discord.Client, channel_id: int | None, key: str, posts=MOD_LOG_POSTS):
    """Where an entry of this kind goes: the forum's post for it, the channel itself when it isn't a forum,
    None when there is nowhere to send (logged, never raised)."""
    if channel_id is None:
        return None
    try:
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        if isinstance(channel, discord.ForumChannel):
            return await _post(bot, channel, key, posts)
        return channel if hasattr(channel, "send") else None
    except Exception:
        log.warning("Can't reach log channel %s (%s)", channel_id, key, exc_info=True)
        return None


async def _post(bot: discord.Client, forum: discord.ForumChannel, key: str, posts) -> discord.Thread:
    slot = (forum.id, key)
    async with _locks.setdefault(slot, asyncio.Lock()):
        thread_id = _threads.get(slot) or await _stored(forum.id, key)
        thread = await _thread(bot, forum, thread_id) if thread_id else None
        if thread is None:
            thread = _named(forum, title(posts, key)) or await _create(forum, key, posts)
            await _store(forum.id, key, thread.id)
        _threads[slot] = thread.id
        if thread.archived and not thread.locked:
            # Sending reopens it on its own, but edits and deletes of older entries need it open.
            thread = await thread.edit(archived=False)
        return thread


async def _thread(bot: discord.Client, forum: discord.ForumChannel, thread_id: int) -> discord.Thread | None:
    """The stored post, or None when it was deleted or moved (it is then made again)."""
    thread = forum.guild.get_thread(thread_id)
    if thread is None:
        try:
            thread = await bot.fetch_channel(thread_id)
        except (discord.NotFound, discord.Forbidden):
            return None
    if not isinstance(thread, discord.Thread) or thread.parent_id != forum.id:
        return None
    return thread


def _named(forum: discord.ForumChannel, name: str) -> discord.Thread | None:
    """A post staff already made with this name, so a new forum can be laid out by hand first."""
    return next((thread for thread in forum.threads if thread.name == name and not thread.locked), None)


async def _create(forum: discord.ForumChannel, key: str, posts) -> discord.Thread:
    emoji, name, about = posts[key]
    tags = [tag for tag in forum.available_tags if tag.name.strip().lower() == name.lower()][:1]
    return await forum.create_thread(
        name=f"{emoji} {name}",
        view=card(f"### {emoji} {name}\n{about}", color=discord.Color.dark_grey()),
        applied_tags=tags,
        auto_archive_duration=ARCHIVE_MINUTES,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def _stored(forum_id: int, key: str) -> int | None:
    return await (await get_pool()).fetchval(
        "SELECT thread_id FROM log_forum_posts WHERE forum_id = $1 AND key = $2", forum_id, key
    )


async def _store(forum_id: int, key: str, thread_id: int) -> None:
    await (await get_pool()).execute(
        """INSERT INTO log_forum_posts (forum_id, key, thread_id) VALUES ($1, $2, $3)
        ON CONFLICT (forum_id, key) DO UPDATE SET thread_id = EXCLUDED.thread_id, created_at = now()""",
        forum_id,
        key,
        thread_id,
    )
