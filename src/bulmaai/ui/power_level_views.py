from collections.abc import Sequence

import discord

from bulmaai.services.member_activity import xp_threshold
from bulmaai.ui.v2 import TEXT_LIMIT, card, facts, head, trim

MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def progress_bar(done: int, total: int, width: int = 10) -> str:
    filled = min(width, round(width * done / max(total, 1)))
    return "▰" * filled + "▱" * (width - filled)


def build_power_level_card(
    *,
    display_name: str,
    avatar_url: str | None,
    xp: int,
    level: int,
) -> discord.ui.DesignerView:
    floor = xp_threshold(level)
    ceiling = xp_threshold(level + 1)
    into_level = xp - floor
    span = max(ceiling - floor, 1)
    name = discord.utils.escape_markdown(display_name)
    top = f"## ⚡ {name}'s Power Level\n" + facts(("Level", level), ("Total XP", f"{xp:,}"))
    progress = (
        f"`{progress_bar(into_level, span)}` {into_level:,} / {span:,} XP ({into_level / span:.0%})\n"
        f"-# to level {level + 1}"
    )
    return card(head(top, avatar_url), progress, color=discord.Color.orange())


def build_leaderboard_card(
    *,
    guild_name: str,
    entries: Sequence[tuple[str, int, int]],
) -> discord.ui.DesignerView:
    title = f"## 🏆 {discord.utils.escape_markdown(guild_name)} Power Levels"
    lines = [
        # Nicknames are member-controlled: escaped so "[Free Nitro](https://…)" can't become a link in a public card.
        f"{MEDALS.get(rank, f'**#{rank}**')} {discord.utils.escape_markdown(display_name)} · Level {level} ({xp:,} XP)"
        for rank, (display_name, xp, level) in enumerate(entries, start=1)
    ]
    body = trim("\n".join(lines), TEXT_LIMIT - len(title)) if lines else "No one has powered up yet."
    return card(title, discord.ui.Separator(), body, color=discord.Color.gold())
