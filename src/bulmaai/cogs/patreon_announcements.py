import json
import logging
import re
from datetime import datetime
from pathlib import Path

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

from bulmaai.config import load_settings
from bulmaai.services.http import request

log = logging.getLogger(__name__)

load_dotenv()
settings = load_settings()

# ── Channel where announcements are posted ──────────────────────────
PATREON_NEWS_CHANNEL_ID = 1287884173054316574

# ── Patreon API v2 base ─────────────────────────────────────────────
PATREON_API = "https://www.patreon.com/api/oauth2/v2"

# ── State file for tracking last seen post ──────────────────────────
STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "patreon_state.json"

# ── Patreon brand colour ────────────────────────────────────────────
PATREON_COLOUR = discord.Colour.from_rgb(255, 85, 0)


# ── Helpers ─────────────────────────────────────────────────────────
def _strip_html(html: str) -> str:
    """Rough HTML → plain-text conversion for Patreon post content."""
    # Replace <br> and </p> with newlines
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"</p>", "\n", text)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Collapse multiple blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _truncate(text: str, limit: int = 300) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0] + "…"


def _build_post_embed(post_data: dict, *, is_public: bool) -> discord.Embed:
    """Build a rich Discord embed from a Patreon post API object."""
    attrs = post_data.get("attributes", {})
    post_id = post_data["id"]

    title = attrs.get("title") or "New Patreon Post"
    content_html = attrs.get("content") or ""
    published_at = attrs.get("published_at")
    url = attrs.get("url") or f"https://www.patreon.com/posts/{post_id}"
    thumbnail = (attrs.get("image") or {}).get("large_url")

    description_text = _strip_html(content_html)
    description_text = _truncate(description_text, 350)

    visibility = "🌐 Public" if is_public else "🔒 Patrons Only"

    embed = discord.Embed(
        title=title,
        url=url,
        description=description_text or "*No text content.*",
        colour=PATREON_COLOUR,
    )
    embed.set_author(
        name="New Patreon Post",
        icon_url="https://c5.patreon.com/external/favicon/favicon-32x32.png",
    )
    if thumbnail:
        embed.set_image(url=thumbnail)

    embed.add_field(name="Visibility", value=visibility, inline=True)

    if published_at:
        try:
            dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
            embed.timestamp = dt
        except ValueError:
            pass

    embed.set_footer(text="Patreon • DragonMineZ")
    return embed


# ── State file helpers ──────────────────────────────────────────────
def _get_last_post_id() -> str:
    if not STATE_FILE.exists():
        return ""
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data.get("last_post_id", "")
    except (json.JSONDecodeError, OSError):
        return ""


def _set_last_post_id(post_id: str) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps({"last_post_id": post_id}, indent=2),
        encoding="utf-8",
    )


# ── Cog ─────────────────────────────────────────────────────────────
class PatreonAnnouncementsCog(commands.Cog):
    """Polls Patreon for new posts and announces them in Discord."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.token: str | None = settings.PATREON_CREATOR_TOKEN
        self.campaign_id: str | None = settings.PATREON_CAMPAIGN_ID

    # ── lifecycle ────────────────────────────────────────────────────
    async def cog_load(self) -> None:
        if not self.token or not self.campaign_id:
            log.warning(
                "Patreon credentials missing – PatreonAnnouncementsCog will NOT poll."
            )
            return

        self.poll_patreon.start()
        log.info("Patreon polling loop started (every 5 min).")

    def cog_unload(self) -> None:
        self.poll_patreon.cancel()

    # ── polling loop ─────────────────────────────────────────────────
    @tasks.loop(minutes=5)
    async def poll_patreon(self) -> None:
        try:
            posts = await self._fetch_recent_posts()
        except Exception:
            log.exception("Failed to fetch Patreon posts")
            return

        if not posts:
            return

        last_id = _get_last_post_id()

        # Posts come sorted newest-first.  Find the ones we haven't seen.
        new_posts: list[dict] = []
        for post in posts:
            if post["id"] == last_id:
                break
            new_posts.append(post)

        if not new_posts:
            return

        # First-run seeding: if we had no stored ID yet, just save the
        # latest post without announcing, to avoid flooding the channel.
        if not last_id:
            log.info("First run – seeding last_post_id with %s", new_posts[0]["id"])
            _set_last_post_id(new_posts[0]["id"])
            return

        # Announce oldest-first so the channel reads chronologically.
        channel = self.bot.get_channel(PATREON_NEWS_CHANNEL_ID)
        if channel is None:
            log.error("Patreon news channel %s not found!", PATREON_NEWS_CHANNEL_ID)
            return

        for post in reversed(new_posts):
            attrs = post.get("attributes", {})
            is_public = attrs.get("is_public", False)

            embed = _build_post_embed(post, is_public=is_public)
            await channel.send(embed=embed)
            log.info("Announced Patreon post %s: %s", post["id"], attrs.get("title"))

        # Store the newest post ID so we don't re-announce.
        _set_last_post_id(new_posts[0]["id"])

    @poll_patreon.before_loop
    async def _before_poll(self) -> None:
        await self.bot.wait_until_ready()

    # ── Patreon API call ─────────────────────────────────────────────
    async def _fetch_recent_posts(self) -> list[dict]:
        """Return the latest posts for the configured campaign (newest first)."""
        url = f"{PATREON_API}/campaigns/{self.campaign_id}/posts"
        headers = {"Authorization": f"Bearer {self.token}"}
        params = {
            "fields[post]": "title,content,url,published_at,image,is_public",
            "sort": "-published_at",
            "page[count]": "5",
        }

        resp = await request("GET", url, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", [])


def setup(bot: discord.Bot):
    bot.add_cog(PatreonAnnouncementsCog(bot))

