import discord


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def build_showcase_highlight_embed(
    *,
    author_name: str,
    author_avatar_url: str | None,
    content: str,
    image_url: str | None,
    reaction_count: int,
    reaction_emoji: str,
    jump_url: str,
) -> discord.Embed:
    embed = discord.Embed(
        description=_truncate(content, 4096) if content else None,
        colour=discord.Colour.gold(),
    )
    embed.set_author(name=author_name, icon_url=author_avatar_url)
    if image_url:
        embed.set_image(url=image_url)
    embed.add_field(name="Reactions", value=f"{reaction_emoji} {reaction_count}", inline=True)
    embed.add_field(name="Original", value=f"[Jump to message]({jump_url})", inline=True)
    return embed
