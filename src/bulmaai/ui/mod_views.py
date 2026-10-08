"""Buttons on moderation messages. The custom id carries the target, so a click still works after a
restart; cogs/mod_interactions.py routes clicks by prefix (these views have no callbacks of their own)."""

import discord

from bulmaai.ui.mod_cards import ALERT

QUICK = "modqa"  # modqa:<action>:<user_id>[:<channel_id>:<message_id>]
APPEAL = "modappeal"  # modappeal:<guild_id>, on the ban DM
APPEAL_REVIEW = "modappeal-review"  # modappeal-review:<accept|deny>:<user_id>
RAID = "modraid"  # modraid:<lockdown|unlock|end>, on raid alerts
TUNE = "modtune"  # modtune:allow:<automod_hit_id>, offered after a false positive on a link filter

QUICK_ACTIONS: dict[str, tuple[str, discord.ButtonStyle]] = {
    "delete": ("Delete message", discord.ButtonStyle.secondary),
    "learn": ("Delete & learn", discord.ButtonStyle.danger),  # image alerts: delete + add to the scam list
    "warn": ("Warn", discord.ButtonStyle.secondary),
    "timeout": ("Timeout 24h", discord.ButtonStyle.primary),
    "untimeout": ("Remove timeout", discord.ButtonStyle.success),
    "kick": ("Kick", discord.ButtonStyle.danger),
    "ban": ("Ban", discord.ButtonStyle.danger),
    "dismiss": ("Dismiss", discord.ButtonStyle.secondary),
    "falsepos": ("False positive", discord.ButtonStyle.secondary),  # automod alerts: undo + tune
}


def _view(*buttons: discord.ui.Button) -> discord.ui.View:
    # ponytail: timeout=None views stay in py-cord's view store until restart; a few bytes per alert.
    view = discord.ui.View(timeout=None)
    for button in buttons:
        view.add_item(button)
    return view


def quick_actions_view(
    user_id: int,
    *,
    actions: tuple[str, ...] = ("timeout", "ban", "dismiss"),
    message: tuple[int, int] | None = None,
) -> discord.ui.View:
    """message=(channel_id, message_id) enables the "delete" and "learn" actions."""
    buttons = []
    for action in actions:
        if action in ("delete", "learn") and message is None:
            continue
        label, style = QUICK_ACTIONS[action]
        custom_id = f"{QUICK}:{action}:{user_id}"
        if action in ("delete", "learn"):
            custom_id += f":{message[0]}:{message[1]}"
        buttons.append(discord.ui.Button(label=label, style=style, custom_id=custom_id))
    return _view(*buttons)


def appeal_view(guild_id: int) -> discord.ui.View:
    return _view(discord.ui.Button(label="Appeal", style=discord.ButtonStyle.primary, custom_id=f"{APPEAL}:{guild_id}"))


def appeal_review_view(user_id: int) -> discord.ui.View:
    return _view(
        discord.ui.Button(label="Unban", style=discord.ButtonStyle.success, custom_id=f"{APPEAL_REVIEW}:accept:{user_id}"),
        discord.ui.Button(label="Deny", style=discord.ButtonStyle.danger, custom_id=f"{APPEAL_REVIEW}:deny:{user_id}"),
    )


def raid_view(*, locked: bool) -> discord.ui.View:
    first = (
        discord.ui.Button(label="Unlock channels", style=discord.ButtonStyle.success, custom_id=f"{RAID}:unlock")
        if locked
        else discord.ui.Button(label="Lock down", style=discord.ButtonStyle.danger, custom_id=f"{RAID}:lockdown")
    )
    return _view(first, discord.ui.Button(label="End raid mode", style=discord.ButtonStyle.secondary, custom_id=f"{RAID}:end"))


def allowlist_view(hit_id: int, domains: tuple[str, ...]) -> discord.ui.View:
    label = f"Allowlist {', '.join(domains[:2])}"[:80]
    return _view(discord.ui.Button(label=label, style=discord.ButtonStyle.primary, custom_id=f"{TUNE}:allow:{hit_id}"))


def parse_custom_id(custom_id: str | None) -> tuple[str, list[str]] | None:
    """('modqa', ['ban', '123']) for our buttons, None for anyone else's."""
    prefix, _, rest = (custom_id or "").partition(":")
    if prefix not in (QUICK, APPEAL, APPEAL_REVIEW, RAID, TUNE, ALERT) or not rest:
        return None
    return prefix, rest.split(":")
