import discord

from bulmaai.ui.v2 import card, head, trim

CONTENT_LIMIT = 3000


def build_showcase_highlight_card(
    *,
    author_name: str,
    author_avatar_url: str | None,
    content: str,
    image_url: str | None,
    reaction_count: int,
    reaction_emoji: str,
    jump_url: str,
) -> discord.ui.DesignerView:
    """content must already be defanged (member text reposted under the bot's name)."""
    top = f"### {reaction_emoji} Showcase highlight\n-# by **{discord.utils.escape_markdown(author_name)}**"
    return card(
        head(top, author_avatar_url),
        trim(content, CONTENT_LIMIT) if content else None,
        discord.ui.MediaGallery(discord.MediaGalleryItem(image_url)) if image_url else None,
        f"-# {reaction_emoji} {reaction_count} reactions",
        color=discord.Colour.gold(),
        buttons=[discord.ui.Button(label="Original message", url=jump_url)],
    )
