from collections.abc import Sequence

import discord

from bulmaai.services.member_activity import xp_threshold


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def build_power_level_embed(
    *,
    display_name: str,
    avatar_url: str | None,
    xp: int,
    level: int,
) -> discord.Embed:
    floor = xp_threshold(level)
    ceiling = xp_threshold(level + 1)
    into_level = xp - floor
    span = max(ceiling - floor, 1)

    embed = discord.Embed(
        title=f"{display_name}'s Power Level",
        color=discord.Color.orange(),
    )
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    embed.add_field(name="Power Level", value=str(level), inline=True)
    embed.add_field(name="Total XP", value=f"{xp:,}", inline=True)
    embed.add_field(
        name="Progress to Next Level",
        value=f"{into_level:,} / {span:,} XP ({into_level / span:.0%})",
        inline=False,
    )
    return embed


def build_leaderboard_embed(
    *,
    guild_name: str,
    entries: Sequence[tuple[str, int, int]],
) -> discord.Embed:
    embed = discord.Embed(
        title=f"{guild_name} Power Level Leaderboard",
        color=discord.Color.gold(),
    )
    if not entries:
        embed.description = "No one has powered up yet."
        return embed

    lines = [
        # Nicknames are member-controlled: escaped so "[Free Nitro](https://…)" can't become a link in a public embed.
        f"**#{rank}** {discord.utils.escape_markdown(display_name)} — Level {level} ({xp:,} XP)"
        for rank, (display_name, xp, level) in enumerate(entries, start=1)
    ]
    embed.description = _truncate("\n".join(lines), 4096)
    return embed
