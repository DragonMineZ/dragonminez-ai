import asyncio
import html
import logging
import re
from datetime import datetime, timezone
from time import monotonic
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import discord
from discord.ext import commands, tasks

from bulmaai.services.http import request
from bulmaai.services.patreon_state import (
    get_patreon_campaign_state,
    upsert_patreon_campaign_state,
)
from bulmaai.utils.permissions import is_admin

logger = logging.getLogger(__name__)

PATREON_API = "https://www.patreon.com/api/oauth2/v2"
PATREON_SITE = "https://www.patreon.com"
PATREON_COLOUR = discord.Colour.from_rgb(255, 85, 0)
PATREON_POST_PAGE_SIZE = 50
PATREON_POST_MAX_PAGES = 20
PATREON_POST_ID_RE = re.compile(r"(?<!\d)(\d{6,})(?!\d)")
PUBLIC_POST_DESCRIPTION_LIMIT = 3500

PATREON_WELCOME_AUDIT_WINDOW_SECONDS = 30
PATREON_WELCOME_CACHE_SECONDS = 120
# Edit the Patreon welcome copy here.
PATREON_WELCOME_CHANNEL_TITLE = "New Patreon!"
PATREON_WELCOME_CHANNEL_DESCRIPTION = (
    "{member} just received {role}. Welcome, and thank you for supporting DragonMineZ!"
)
PATREON_WELCOME_CHANNEL_ROLE_LABEL = "Role"
PATREON_WELCOME_CHANNEL_FOOTER = "DragonMineZ"
PATREON_WELCOME_DM_TITLE = "Welcome to DragonMineZ - Patreon"
PATREON_WELCOME_DM_DESCRIPTION = (
    "Hi {member_name}, thanks for joining the DragonMineZ Patreon! "
    "You now have {role} ({role_name}) in the server, we're glad to have you here! "
    "If your perk is Supporter, it does not include beta access. "
    "To play the beta, you need the Contributor role/perk, which is USD $9.99. "
    "If your perk is Contributor or Benefactor, you can ask for beta access in any channel on the DMZ server; "
    "just ask however you want, and the bot can recognize beta access requests from messages or images. "
    "Your support is invaluable, thank you again!"
)
PATREON_WELCOME_DM_FOOTER = "DragonMineZ Patreon"


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


def _normalize_post_url(post_data: dict) -> str:
    attrs = post_data.get("attributes", {})
    post_id = str(post_data["id"])
    raw_url = (attrs.get("url") or "").strip()
    if raw_url:
        if raw_url.startswith(("http://", "https://")):
            return raw_url
        return urljoin(f"{PATREON_SITE}/", raw_url)
    return f"{PATREON_SITE}/posts/{post_id}"


def _extract_post_id(reference: str) -> str | None:
    value = (reference or "").strip()
    if not value:
        return None
    if value.isdigit():
        return value

    match = PATREON_POST_ID_RE.search(value)
    if match:
        return match.group(1)
    return None


def _strip_html(html: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    text = re.sub(r"</p>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rsplit(" ", 1)[0] + "..."


def _get_embed_data(attrs: dict) -> dict:
    embed_data = attrs.get("embed_data")
    return embed_data if isinstance(embed_data, dict) else {}


def _extract_embed_image_url(embed_data: dict) -> str | None:
    html_snippet = embed_data.get("html")
    if not html_snippet:
        return None

    match = re.search(r'src="([^"]+)"', html_snippet)
    if not match:
        return None

    iframe_src = html.unescape(match.group(1))
    if iframe_src.startswith("//"):
        iframe_src = "https:" + iframe_src

    image_values = parse_qs(urlparse(iframe_src).query).get("image")
    if not image_values:
        return None

    image_url = unquote(image_values[0])
    if image_url.startswith("//"):
        image_url = "https:" + image_url
    if image_url.startswith(("http://", "https://")):
        return image_url
    return None


def _build_post_embed(post_data: dict, *, is_public: bool) -> discord.Embed:
    attrs = post_data.get("attributes", {})
    title = attrs.get("title") or "New Patreon Post"
    url = _normalize_post_url(post_data)
    content = attrs.get("content") or ""
    content_text = _strip_html(content)
    embed_data = _get_embed_data(attrs)
    embed_subject = (embed_data.get("subject") or "").strip()
    embed_description = (embed_data.get("description") or "").strip()
    embed_provider = (embed_data.get("provider") or "").strip()

    if is_public:
        description_source = content_text or embed_description or "A new public Patreon post is live."
        if content_text and embed_description and embed_description not in content_text:
            description_source = f"{content_text}\n\n{embed_description}"
        description = description_source
        description = _truncate(description, PUBLIC_POST_DESCRIPTION_LIMIT)
        visibility = "Public"
    else:
        description = (
            "A new Patreon post is live. The Patreon link below is included so Discord can show only the public-safe preview Patreon exposes."
        )
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

    if is_public and (embed_subject or embed_provider):
        media_summary = embed_subject or "Embedded media available"
        if embed_provider:
            media_summary = f"{media_summary}\nProvider: {embed_provider}"
        embed.add_field(name="Embedded Media", value=_truncate(media_summary, 1024), inline=False)

    if is_public:
        image_url = _extract_embed_image_url(embed_data)
        if image_url:
            embed.set_image(url=image_url)

    published_at = _parse_published_at(attrs.get("published_at"))
    if published_at is not None:
        embed.timestamp = published_at

    embed.set_footer(text="Patreon | DragonMineZ")
    return embed


def _build_post_view(post_data: dict) -> discord.ui.View | None:
    attrs = post_data.get("attributes", {})
    url = _normalize_post_url(post_data)
    if not url:
        return None

    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Open on Patreon", url=url))

    if attrs.get("is_public"):
        embed_url = (attrs.get("embed_url") or "").strip()
        if embed_url and embed_url.startswith(("http://", "https://")) and embed_url != url:
            provider = (_get_embed_data(attrs).get("provider") or "").strip()
            label = f"Open {provider}" if provider else "Open Embedded Media"
            view.add_item(discord.ui.Button(label=_truncate(label, 80), url=embed_url))

    return view


def _render_welcome_text(template: str, *, member: discord.Member, role: discord.Role) -> str:
    replacements = {
        "{member}": member.mention,
        "{member_name}": member.display_name,
        "{role}": role.mention,
        "{role_name}": role.name,
        "{server_name}": member.guild.name,
    }
    rendered = template
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def _build_channel_welcome_embed(*, member: discord.Member, role: discord.Role) -> discord.Embed:
    embed = discord.Embed(
        title=_render_welcome_text(PATREON_WELCOME_CHANNEL_TITLE, member=member, role=role) or None,
        description=_render_welcome_text(
            PATREON_WELCOME_CHANNEL_DESCRIPTION,
            member=member,
            role=role,
        ) or None,
        colour=PATREON_COLOUR,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(
        name=_render_welcome_text(PATREON_WELCOME_CHANNEL_ROLE_LABEL, member=member, role=role) or "Role",
        value=role.mention,
        inline=False,
    )
    footer = _render_welcome_text(PATREON_WELCOME_CHANNEL_FOOTER, member=member, role=role)
    if footer:
        embed.set_footer(text=footer)
    return embed


def _build_dm_welcome_embed(*, member: discord.Member, role: discord.Role) -> discord.Embed:
    embed = discord.Embed(
        title=_render_welcome_text(PATREON_WELCOME_DM_TITLE, member=member, role=role) or None,
        description=_render_welcome_text(
            PATREON_WELCOME_DM_DESCRIPTION,
            member=member,
            role=role,
        ) or None,
        colour=PATREON_COLOUR,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    footer = _render_welcome_text(PATREON_WELCOME_DM_FOOTER, member=member, role=role)
    if footer:
        embed.set_footer(text=footer)
    return embed


class PatreonAnnouncementsCog(commands.Cog):
    """Polls Patreon for new posts and announces them in Discord."""

    patreon = discord.SlashCommandGroup("patreon", "Patreon announcement tools")

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.settings = bot.settings
        self.token = self.settings.PATREON_CREATOR_TOKEN
        self.campaign_id = self.settings.PATREON_CAMPAIGN_ID
        self.channel_id = self.settings.patreon_announcement_channel_id
        self._poll_lock = asyncio.Lock()
        self._poll_started = False
        self._recent_welcome_events: dict[tuple[int, tuple[int, ...]], float] = {}

    def _cleanup_recent_welcome_events(self) -> None:
        now = monotonic()
        for key, timestamp in list(self._recent_welcome_events.items()):
            if now - timestamp > PATREON_WELCOME_CACHE_SECONDS:
                self._recent_welcome_events.pop(key, None)

    def _is_recent_welcome_event(self, member_id: int, roles: list[discord.Role]) -> bool:
        self._cleanup_recent_welcome_events()
        role_ids = tuple(sorted(role.id for role in roles))
        return (member_id, role_ids) in self._recent_welcome_events

    def _mark_recent_welcome_event(self, member_id: int, roles: list[discord.Role]) -> None:
        role_ids = tuple(sorted(role.id for role in roles))
        self._recent_welcome_events[(member_id, role_ids)] = monotonic()

    async def _resolve_patreon_welcome_channel(self) -> discord.abc.Messageable | None:
        channel_id = self.bot.settings.patreon_welcome_channel_id
        if channel_id is None:
            return None

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception:
                logger.exception("Failed to fetch Patreon welcome channel %s", channel_id)
                return None

        return channel if hasattr(channel, "send") else None

    async def _find_patreon_role_update(
        self,
        member: discord.Member,
    ) -> discord.AuditLogEntry | None:
        patreon_bot_user_id = self.bot.settings.patreon_bot_user_id
        if patreon_bot_user_id is None:
            return None

        for attempt in range(3):
            try:
                async for entry in member.guild.audit_logs(
                    limit=6,
                    action=discord.AuditLogAction.member_role_update,
                ):
                    if getattr(entry.target, "id", None) != member.id:
                        continue
                    if getattr(entry.user, "id", None) != patreon_bot_user_id:
                        continue
                    age_seconds = (datetime.now(timezone.utc) - entry.created_at).total_seconds()
                    if age_seconds <= PATREON_WELCOME_AUDIT_WINDOW_SECONDS:
                        return entry
            except discord.Forbidden:
                logger.warning("Missing permission to read audit logs for Patreon welcome detection.")
                return None
            except Exception:
                logger.exception("Failed to inspect audit logs for Patreon welcome detection.")
                return None

            if attempt < 2:
                await asyncio.sleep(1)

        return None

    async def _send_patreon_welcome(self, member: discord.Member, role: discord.Role) -> None:
        welcome_channel = await self._resolve_patreon_welcome_channel()
        if welcome_channel is not None:
            await welcome_channel.send(
                embed=_build_channel_welcome_embed(member=member, role=role),
                allowed_mentions=discord.AllowedMentions.none(),
            )

        try:
            await member.send(
                embed=_build_dm_welcome_embed(member=member, role=role),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            logger.info("Could not DM Patreon welcome message to user %s", member.id)
        except Exception:
            logger.exception("Failed to DM Patreon welcome message to user %s", member.id)

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

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if after.bot or before.roles == after.roles:
            return

        added_roles = [
            role
            for role in after.roles
            if role not in before.roles and role != after.guild.default_role
        ]
        if not added_roles:
            return
        if self._is_recent_welcome_event(after.id, added_roles):
            return

        audit_entry = await self._find_patreon_role_update(after)
        if audit_entry is None:
            return

        self._mark_recent_welcome_event(after.id, added_roles)
        primary_role = max(added_roles, key=lambda role: (role.position, role.id))
        await self._send_patreon_welcome(after, primary_role)

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
            await self._announce_post_to_channel(channel, post)

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
            "fields[post]": "title,url,published_at,is_public,content,embed_url,embed_data",
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

    async def _fetch_post_by_id(self, post_id: str) -> dict:
        url = f"{PATREON_API}/posts/{post_id}"
        headers = {"Authorization": f"Bearer {self.token}"}
        params = {
            "fields[post]": "title,url,published_at,is_public,content,embed_url,embed_data",
        }

        resp = await request("GET", url, headers=headers, params=params)
        resp.raise_for_status()
        payload = resp.json()
        return payload["data"]

    async def _resolve_announcement_channel(self) -> discord.abc.Messageable | None:
        return await self._resolve_target_channel()

    async def _announce_post_to_channel(
        self,
        channel: discord.abc.Messageable,
        post_data: dict,
    ) -> None:
        attrs = post_data.get("attributes", {})
        post_url = _normalize_post_url(post_data)
        await channel.send(
            content=post_url,
            embed=_build_post_embed(post_data, is_public=bool(attrs.get("is_public"))),
            view=_build_post_view(post_data),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        logger.info("Announced Patreon post %s: %s", post_data["id"], attrs.get("title"))

    async def _store_latest_post(self, post_data: dict) -> None:
        attrs = post_data.get("attributes", {})
        await upsert_patreon_campaign_state(
            campaign_id=self.campaign_id,
            post_id=str(post_data["id"]),
            post_title=attrs.get("title"),
            post_url=_normalize_post_url(post_data),
            published_at=_parse_published_at(attrs.get("published_at")),
        )

    async def _update_state_if_newer(self, post_data: dict) -> bool:
        current_state = await get_patreon_campaign_state(self.campaign_id)
        if current_state is None or current_state.last_processed_post_id is None:
            await self._store_latest_post(post_data)
            return True

        current_time = current_state.last_processed_at or datetime.min.replace(tzinfo=timezone.utc)
        post_time = _parse_published_at((post_data.get("attributes") or {}).get("published_at"))
        if post_time is None:
            post_time = datetime.min.replace(tzinfo=timezone.utc)

        current_key = (current_time, str(current_state.last_processed_post_id))
        post_key = (post_time, str(post_data["id"]))
        if post_key >= current_key:
            await self._store_latest_post(post_data)
            return True
        return False

    @patreon.command(name="manual_post", description="Manually announce a Patreon post by URL or ID")
    @discord.option(
        "post_reference",
        description="Patreon post ID or Patreon post URL",
        required=True,
    )
    @discord.option(
        "record_state",
        description="Advance Patreon state if this post is the newest known post",
        required=False,
        default=True,
    )
    async def manual_post(
        self,
        ctx: discord.ApplicationContext,
        post_reference: str,
        record_state: bool = True,
    ) -> None:
        author = ctx.author if isinstance(ctx.author, discord.Member) else None
        if author is None or not is_admin(author):
            await ctx.respond("Only staff can manually post Patreon announcements.", ephemeral=True)
            return

        post_id = _extract_post_id(post_reference)
        if post_id is None:
            await ctx.respond(
                "Could not determine a Patreon post ID from that value. Use a numeric Patreon post ID or a Patreon post URL.",
                ephemeral=True,
            )
            return

        await ctx.defer(ephemeral=True)

        try:
            post_data = await self._fetch_post_by_id(post_id)
        except Exception:
            logger.exception("Failed to fetch Patreon post %s for manual post", post_id)
            await ctx.followup.send(
                f"Failed to fetch Patreon post `{post_id}` from Patreon.",
                ephemeral=True,
            )
            return

        channel = await self._resolve_announcement_channel()
        if channel is None:
            await ctx.followup.send(
                "The Patreon announcement channel could not be resolved.",
                ephemeral=True,
            )
            return

        try:
            await self._announce_post_to_channel(channel, post_data)
        except Exception:
            logger.exception("Failed to manually announce Patreon post %s", post_id)
            await ctx.followup.send(
                f"Failed to send Patreon post `{post_id}` to Discord.",
                ephemeral=True,
            )
            return

        state_updated = False
        if record_state:
            state_updated = await self._update_state_if_newer(post_data)

        post_url = _normalize_post_url(post_data)
        if record_state:
            if state_updated:
                state_message = "Campaign state was updated."
            else:
                state_message = "Campaign state was left unchanged because a newer post is already recorded."
        else:
            state_message = "Campaign state was not changed."

        await ctx.followup.send(
            f"Manually posted Patreon post `{post_id}` to <#{self.channel_id}>.\n{post_url}\n{state_message}",
            ephemeral=True,
        )


def setup(bot: discord.Bot) -> None:
    bot.add_cog(PatreonAnnouncementsCog(bot))
