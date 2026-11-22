"""Notion Tracker -> staff forum feed. One forum post per Tracker item (services/notion_feed): the starter card is
edited on every change, status/priority changes get a short note, Notion comments are posted under it, done items are
archived and trashed ones archived and locked. One-way: nothing here writes to Notion, and nobody is ever pinged.

Notion's webhooks (POST /notion-webhook) may come aggregated, retried or out of order, so an event only says which page
to look at: the page is always fetched and compared with the stored row."""

import asyncio
import json
import logging
from collections import deque

import discord

from bulmaai.services import notion, notion_feed
from bulmaai.services.release_webhook import (
    ReleaseWebhookHttpResponse,
    register_extra_raw_webhook_route,
    text_http_response,
    unregister_extra_raw_webhook_route,
)
from bulmaai.ui import notion_cards
from bulmaai.utils.lifecycle import ReloadableCog

log = logging.getLogger(__name__)

ROUTE_PATH = "/notion-webhook"
PAGE_EVENTS = {"page.created", "page.properties_updated", "page.deleted", "page.undeleted"}
EVENTS = PAGE_EVENTS | {"comment.created"}
SEEN_EVENTS = 500
NO_PINGS = discord.AllowedMentions.none()


class NotionFeedCog(ReloadableCog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._route_path: str | None = None
        # ponytail: one lock for every event; per-page locks if the Tracker ever gets busy enough to matter.
        self._lock = asyncio.Lock()
        self._seen: deque[str] = deque(maxlen=SEEN_EVENTS)
        self._tags_ready: set[int] = set()  # forums whose feed tags were checked this process

    @property
    def settings(self):
        return self.__dict__.get("_settings_override") or self.bot.settings

    @settings.setter
    def settings(self, value) -> None:
        # Tests build the cog without a bot and pin settings directly.
        self.__dict__["_settings_override"] = value

    # --- lifecycle ------------------------------------------------------------------------------

    async def on_startup(self) -> None:
        self._register_webhook_route()

    async def on_shutdown(self) -> None:
        if self._route_path is not None:
            unregister_extra_raw_webhook_route(self._route_path)
            self._route_path = None

    def _register_webhook_route(self) -> None:
        if self._route_path is not None:
            return
        if not self.settings.notion_token or not self.settings.notion_forum_channel_id:
            log.info("Notion forum feed off (needs NOTION_TOKEN and notion_forum_channel_id).")
            return
        loop = asyncio.get_running_loop()
        register_extra_raw_webhook_route(
            path=ROUTE_PATH, handle_request=lambda body, headers: self.handle(body, headers, loop)
        )
        self._route_path = ROUTE_PATH

    # --- webhook (runs on the webhook server's thread) ------------------------------------------

    def handle(self, body: bytes, headers, loop: asyncio.AbstractEventLoop) -> ReleaseWebhookHttpResponse:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return text_http_response(400, "Invalid JSON body")
        if not isinstance(payload, dict):
            return text_http_response(400, "Invalid JSON body")
        if "verification_token" in payload and "type" not in payload:
            # Notion's one-time subscription handshake: Bruno copies this into Notion and into NOTION_WEBHOOK_TOKEN.
            log.info("Notion webhook verification token: %s", payload.get("verification_token"))
            return text_http_response(200, "OK")
        token = self.settings.notion_webhook_token
        if not token:
            return text_http_response(503, "Not configured")
        if not notion.verify_webhook(token, body, headers.get("X-Notion-Signature")):
            return text_http_response(401, "Bad signature")
        if payload.get("type") not in EVENTS:
            return text_http_response(200, "Ignored")
        future = asyncio.run_coroutine_threadsafe(self.process(payload), loop)

        def log_result(done: "asyncio.Future[None]") -> None:
            try:
                done.result()
            except Exception:
                log.exception("Notion feed event failed")

        future.add_done_callback(log_result)
        return text_http_response(200, "Queued")

    # --- processing -----------------------------------------------------------------------------

    async def process(self, event: dict) -> None:
        async with self._lock:
            event_id = event.get("id")
            if event_id:
                if event_id in self._seen:
                    return
                self._seen.append(event_id)
            if event.get("type") == "comment.created":
                return await self._on_comment(event)
            page_id = (event.get("entity") or {}).get("id")
            if page_id:
                await self._on_page(event, page_id)

    async def _author(self, event: dict) -> str:
        authors = event.get("authors") or []
        first = authors[0] if authors and isinstance(authors[0], dict) else {}
        if first.get("type") == "bot":
            return "Notion automation"
        if first.get("type") == "person":
            return await notion.user_name(self.settings.notion_token, first.get("id")) or "Someone"
        return "Someone"

    async def _on_page(self, event: dict, page_id: str) -> None:
        post = await notion_feed.get_post(page_id)
        try:
            page = await notion.get_page(self.settings.notion_token, page_id)
        except notion.NotionError:
            page = None
        if page is None:
            if event.get("type") == "page.deleted" and post is not None:
                await self._deleted(post, await self._author(event))
            return
        item = notion_feed.item_from_page(page)
        if not notion_feed.same_id(item["parent"], self.settings.notion_tracker_data_source_id):
            return
        if item["in_trash"]:
            if post is not None:
                await self._deleted(post, await self._author(event))
            return
        await self._sync(item, post, event)

    async def _deleted(self, post: notion_feed.Post, author: str) -> None:
        if post.status == notion_feed.DELETED:
            return
        thread = await self._thread(post.thread_id)
        if thread is None:
            return await notion_feed.delete_post(post.page_id)
        if thread.archived:
            await thread.edit(archived=False)
        await thread.send(view=notion_cards.deleted_note(author), allowed_mentions=NO_PINGS)
        await thread.edit(archived=True, locked=True)
        await notion_feed.save_post(
            post.page_id, post.thread_id, status=notion_feed.DELETED, priority=post.priority, title=post.title
        )

    async def _sync(self, item: dict, post: notion_feed.Post | None, event: dict) -> None:
        forum = await self._forum()
        if forum is None:
            return
        thread = await self._thread(post.thread_id) if post is not None else None
        if post is not None and thread is None:
            await notion_feed.delete_post(post.page_id)
            post = None
        done = item["status"] in notion_feed.DONE
        if post is None:
            if not done:
                await self._create(forum, item)
            return
        moved = post.status != item["status"] and post.status != notion_feed.DELETED  # a restore needs no move note
        reprioritized = post.priority != item["priority"]
        author = await self._author(event) if moved or reprioritized else ""
        notes = []
        if moved:
            notes.append(notion_cards.status_note(author, post.status, item["status"]))
        if reprioritized:
            notes.append(notion_cards.priority_note(author, post.priority, item["priority"]))
        tags = await self._tags(forum, item)
        # An archived post takes no edits; open it first and archive again at the end if the item is done.
        if thread.archived or thread.locked:
            await thread.edit(archived=False, locked=False)
        await thread.get_partial_message(thread.id).edit(view=notion_cards.item_card(item), allowed_mentions=NO_PINGS)
        for view in notes:
            await thread.send(view=view, allowed_mentions=NO_PINGS)
        changes: dict = {}
        name = notion_feed.post_name(item)
        if thread.name != name:  # renames are rate limited hard; only when the title really changed
            changes["name"] = name
        if {tag.id for tag in thread.applied_tags or []} != {tag.id for tag in tags}:
            changes["applied_tags"] = tags
        if done:
            changes["archived"] = True
        if changes:
            await thread.edit(**changes)
        await notion_feed.save_post(
            item["id"], thread.id, status=item["status"], priority=item["priority"], title=item["title"]
        )

    async def _create(self, forum: discord.ForumChannel, item: dict) -> None:
        thread = await forum.create_thread(
            name=notion_feed.post_name(item),
            view=notion_cards.item_card(item),
            applied_tags=await self._tags(forum, item),
            allowed_mentions=NO_PINGS,
        )
        await notion_feed.save_post(
            item["id"], thread.id, status=item["status"], priority=item["priority"], title=item["title"]
        )

    async def _on_comment(self, event: dict) -> None:
        token = self.settings.notion_token
        data = event.get("data") or {}
        comment_id = (event.get("entity") or {}).get("id")
        parent = data.get("parent") or {}
        page_id = data.get("page_id") or (parent.get("id") if parent.get("type") == "page" else None)
        comment = None
        if not page_id and comment_id:
            comment = await self._comment(comment_id)
            page_id = comment and comment.get("page_id")
        post = await notion_feed.get_post(page_id) if page_id else None
        if post is None or post.status == notion_feed.DELETED:
            return
        authors = event.get("authors") or []
        if any(isinstance(author, dict) and author.get("type") == "bot" for author in authors):
            return
        comment = comment or (await self._comment(comment_id) if comment_id else None)
        if not comment or comment.get("author_type") == "bot" or not comment.get("text", "").strip():
            return
        name = await notion.user_name(token, comment.get("author_id")) or await self._author(event)
        thread = await self._thread(post.thread_id)
        if thread is None:
            return
        await thread.send(view=notion_cards.comment_note(name, comment["text"]), allowed_mentions=NO_PINGS)
        if post.status in notion_feed.DONE:
            await thread.edit(archived=True)  # posting into an archived post opened it

    async def _comment(self, comment_id: str) -> dict | None:
        try:
            return await notion.get_comment(self.settings.notion_token, comment_id)
        except notion.NotionError:
            return None

    # --- Discord --------------------------------------------------------------------------------

    async def _forum(self) -> discord.ForumChannel | None:
        channel_id = self.settings.notion_forum_channel_id
        if not channel_id:
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                log.info("Notion feed forum %s can't be fetched", channel_id)
                return None
        if not isinstance(channel, discord.ForumChannel):
            log.info("Notion feed channel %s isn't a forum", channel_id)
            return None
        return channel

    async def _thread(self, thread_id: int):
        """The item's post, or None when Discord says it's gone (other errors propagate, so a row is never dropped on
        a hiccup)."""
        forum = await self._forum()
        thread = forum.guild.get_thread(thread_id) if forum is not None and forum.guild is not None else None
        if thread is not None:
            return thread
        try:
            return await self.bot.fetch_channel(thread_id)
        except discord.NotFound:
            return None

    async def _tags(self, forum: discord.ForumChannel, item: dict) -> list[discord.ForumTag]:
        """The item's forum tags; the first call per forum adds the feed's missing tags, keeping the forum's own."""
        if forum.id not in self._tags_ready:
            existing = list(forum.available_tags or [])
            have = {tag.name for tag in existing}
            missing = [
                discord.ForumTag(name=name, emoji=emoji) for name, emoji in notion_feed.TAG_EMOJI.items() if name not in have
            ]
            if missing:
                try:
                    forum = await forum.edit(available_tags=existing + missing) or forum
                except discord.HTTPException:
                    log.info("Couldn't add the Notion feed tags to forum %s", forum.id, exc_info=True)
            self._tags_ready.add(forum.id)
        by_name = {tag.name: tag for tag in forum.available_tags or []}
        return [by_name[name] for name in notion_feed.tags_for(item) if name in by_name]


def setup(bot: discord.Bot):
    bot.add_cog(NotionFeedCog(bot))
