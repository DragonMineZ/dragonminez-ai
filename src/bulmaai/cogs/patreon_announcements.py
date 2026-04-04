import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

from bulmaai.services.http import request
from bulmaai.services.patreon_state import (
    get_patreon_campaign_state,
    upsert_patreon_campaign_state,
)

logger = logging.getLogger(__name__)

PATREON_API = "https://www.patreon.com/api/oauth2/v2"
PATREON_COLOUR = discord.Colour.from_rgb(255, 85, 0)
PATREON_POST_PAGE_SIZE = 50
PATREON_POST_MAX_PAGES = 20


def _parse_published_at(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _post_sort_key(post_data: dict) -> tuple[datetime, str]:
    attrs = post_data.get("attributes", {})
    published_at = _parse_published_at(attrs.get("published_at"))
    if published_at is None:
        published_at = datetime.min.replace(tzinfo=timezone.utc)
    return published_at, str(post_data.get("id", ""))


def _build_post_embed(post_data: dict, *, is_public: bool) -> discord.Embed:
    attrs = post_data.get("attributes", {})
    post_id = post_data["id"]
    title = attrs.get("title") or "New Patreon Post"
    url = attrs.get("url") or f"https://www.patreon.com/posts/{post_id}"

    if is_public:
        description = "A new Patreon post is live. This announcement shares the public title and link only."
        visibility = "Public"
    else:
        description = "A new Patreon post is live. This public announcement shares the title and Patreon link only."
        visibility = "Patrons Only"

    embed = discord.Embed(
        title=title,
        url=url,
        description=description,
        colour=PATREON_COLOUR,
    )
    embed.set_author(
        name="New Patreon Post",
        icon_url="https://c5.patreon.com/external/favicon/favicon-32x32.png",
    )
    embed.add_field(name="Visibility", value=visibility, inline=True)

    published_at = _parse_published_at(attrs.get("published_at"))
    if published_at is not None:
        embed.timestamp = published_at

    embed.set_footer(text="Patreon | DragonMineZ")
    return embed


def _build_post_view(post_data: dict) -> discord.ui.View | None:
    url = (post_data.get("attributes") or {}).get("url")
    if not url:
        return None

    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Open on Patreon", url=url))
    return view


class PatreonAnnouncementsCog(commands.Cog):
    """Polls Patreon for new posts and announces them in Discord."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.settings = bot.settings
        self.token = self.settings.PATREON_CREATOR_TOKEN
        self.campaign_id = self.settings.PATREON_CAMPAIGN_ID
        self.channel_id = self.settings.patreon_announcement_channel_id
        self._poll_lock = asyncio.Lock()
        self._poll_started = False

    def _start_polling_if_configured(self) -> None:
        if self._poll_started or self.poll_patreon.is_running():
            return
        if not self.token or not self.campaign_id:
            logger.warning(
                "Patreon credentials missing; PatreonAnnouncementsCog will not poll."
            )
            return
        if self.channel_id is None:
            logger.warning(
                "Patreon announcement channel missing; PatreonAnnouncementsCog will not poll."
            )
            return

        self.poll_patreon.start()
        self._poll_started = True
        logger.info("Patreon polling loop started (every 5 min).")

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        self._start_polling_if_configured()

    def cog_unload(self) -> None:
        self.poll_patreon.cancel()

    @tasks.loop(minutes=5)
    async def poll_patreon(self) -> None:
        async with self._poll_lock:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Failed to poll Patreon posts")

    @poll_patreon.before_loop
    async def _before_poll(self) -> None:
        await self.bot.wait_until_ready()

    async def _poll_once(self) -> None:
        posts = await self._fetch_recent_posts()
        if not posts:
            return

        posts.sort(key=_post_sort_key)
        newest_post = posts[-1]
        newest_post_id = str(newest_post["id"])
        state = await get_patreon_campaign_state(self.campaign_id)
        last_id = state.last_processed_post_id if state is not None else None

        if not last_id:
            logger.info("First Patreon run; seeding last_post_id with %s", newest_post_id)
            await self._store_latest_post(newest_post)
            return

        try:
            last_seen_index = next(
                index for index, post in enumerate(posts) if str(post["id"]) == last_id
            )
        except StopIteration:
            logger.warning(
                "Stored Patreon post id %s was not found in fetched results; reseeding with %s.",
                last_id,
                newest_post_id,
            )
            await self._store_latest_post(newest_post)
            return

        new_posts = posts[last_seen_index + 1:]
        if not new_posts:
            return

        channel = await self._resolve_target_channel()
        if channel is None:
            logger.error(
                "Patreon announcement channel %s could not be resolved.",
                self.channel_id,
            )
            return

        for post in new_posts:
            attrs = post.get("attributes", {})
            await channel.send(
                embed=_build_post_embed(post, is_public=bool(attrs.get("is_public"))),
                view=_build_post_view(post),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            logger.info("Announced Patreon post %s: %s", post["id"], attrs.get("title"))

        await self._store_latest_post(newest_post)

    async def _resolve_target_channel(self) -> discord.abc.Messageable | None:
        if self.channel_id is None:
            return None

        channel = self.bot.get_channel(self.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(self.channel_id)
            except Exception:
                logger.exception(
                    "Failed to fetch Patreon announcement channel %s",
                    self.channel_id,
                )
                return None

        return channel if hasattr(channel, "send") else None

    async def _fetch_recent_posts(self) -> list[dict]:
        """Return the campaign posts fetched through cursor pagination."""
        url = f"{PATREON_API}/campaigns/{self.campaign_id}/posts"
        headers = {"Authorization": f"Bearer {self.token}"}
        params = {
            "fields[post]": "title,url,published_at,is_public",
            "page[count]": str(PATREON_POST_PAGE_SIZE),
        }

        posts: list[dict] = []
        cursor: str | None = None
        page_count = 0

        while page_count < PATREON_POST_MAX_PAGES:
            page_params = dict(params)
            if cursor:
                page_params["page[cursor]"] = cursor

            resp = await request("GET", url, headers=headers, params=page_params)
            resp.raise_for_status()

            payload = resp.json()
            posts.extend(payload.get("data", []))

            pagination = (payload.get("meta") or {}).get("pagination") or {}
            cursors = pagination.get("cursors") or {}
            cursor = cursors.get("next")
            page_count += 1

            if not cursor:
                break

        if cursor:
            logger.warning(
                "Patreon post fetch stopped after %s pages; older posts were truncated.",
                PATREON_POST_MAX_PAGES,
            )

        return posts

    async def _store_latest_post(self, post_data: dict) -> None:
        attrs = post_data.get("attributes", {})
        await upsert_patreon_campaign_state(
            campaign_id=self.campaign_id,
            post_id=str(post_data["id"]),
            post_title=attrs.get("title"),
            post_url=attrs.get("url"),
            published_at=_parse_published_at(attrs.get("published_at")),
        )


def setup(bot: discord.Bot) -> None:
    bot.add_cog(PatreonAnnouncementsCog(bot))
