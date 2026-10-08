"""Components V2 moderation cards: case log cards, command replies and the ids automod alert cards share.
Pure builders (no I/O). Buttons carry their target in the custom id so clicks survive restarts:
modcase:<edit|undo|history>:<case_id> (cogs/mod_case_cards.py), modcard:<show|hide> (cogs/mod_interactions.py)."""

import re
from datetime import datetime

import discord

from bulmaai.services.ai_guard import defang
from bulmaai.services.mod_cases import ModCase

CASE = "modcase"
ALERT = "modcard"

# Fixed component ids on automod alert cards, so a click handler can find its parts after a restart.
ALERT_SUMMARY_ID = 900  # the one-line summary shown once collapsed
ALERT_HANDLED_ID = 901  # "✅ Timed out 7d by @x" lines
ALERT_ACTIONS_ID = 902  # the quick-action button row
ALERT_SHOW = f"{ALERT}:show"
ALERT_HIDE = f"{ALERT}:hide"
ALERT_RESERVED_IDS = (ALERT_SUMMARY_ID, ALERT_HANDLED_ID, ALERT_ACTIONS_ID)
ALERT_HANDLED_MAX = 1000
ALERT_QUOTE_MAX = 400

DEFAULT_AVATAR = "https://cdn.discordapp.com/embed/avatars/0.png"

# action -> (emoji, title, accent colour)
CASE_STYLE: dict[str, tuple[str, str, discord.Color]] = {
    "warn": ("⚠️", "Warn", discord.Color.gold()),
    "note": ("📝", "Note", discord.Color.blurple()),
    "timeout": ("🔇", "Mute", discord.Color.orange()),
    "untimeout": ("🔊", "Unmute", discord.Color.green()),
    "kick": ("👢", "Kick", discord.Color.dark_orange()),
    "softban": ("🧹", "Softban", discord.Color.dark_orange()),
    "ban": ("🔨", "Ban", discord.Color.red()),
    "unban": ("🕊️", "Unban", discord.Color.green()),
}
# What "Undo" does per action, and how an ended case reads.
UNDO_LABELS = {"warn": ("Remove warn", "🗑️"), "timeout": ("Remove timeout", "🔊"), "ban": ("Unban", "🕊️")}
ENDED_VERBS = {"warn": "Removed", "note": "Removed", "timeout": "Lifted", "ban": "Unbanned"}


def _ts(when: datetime, style: str = "R") -> str:
    return discord.utils.format_dt(when, style)


def case_title(case: ModCase) -> str:
    emoji, title, _ = CASE_STYLE.get(case.action, ("📋", case.action.title(), discord.Color.blurple()))
    if case.action == "ban" and (case.duration_seconds or case.expires_at):
        title = "Tempban"
    return f"{emoji} Case {case.id} · {title if case.active else f'~~{title}~~'}"


def _status_line(case: ModCase) -> str | None:
    if case.active:
        return f"⏳ Ends {_ts(case.expires_at)}" if case.expires_at else None
    if case.end_note == "expired":
        return f"⌛ Expired {_ts(case.ended_at or case.expires_at or case.created_at)}"
    if case.ended_at is None:
        return "✅ No longer active"
    line = f"✅ {ENDED_VERBS.get(case.action, 'Ended')} {_ts(case.ended_at)}"
    if case.ended_by:
        line += f" by <@{case.ended_by}>"
    if case.end_note:
        line += f" · *{discord.utils.escape_markdown(case.end_note)}*"
    return line


def case_card(
    case: ModCase, *, name: str, avatar_url: str | None, length: str | None, buttons: bool = True
) -> discord.ui.DesignerView:
    """The mod-log card: Dyno's layout plus the context line, live status and Edit/Undo/History buttons.
    name is the target's escaped username; length is the formatted duration (None when there isn't one)."""
    moderator = f"<@{case.moderator_id}>" if case.moderator_id else "BulmaAI (automatic)"
    body = f"**User** <@{case.user_id}>　**Moderator** {moderator}"
    if length:
        body += f"　**Length** {length}"
    reason = discord.utils.escape_markdown((case.reason or "No reason given")[:900])
    text = f"### {case_title(case)} · {name}\n{body}\n**Reason** {reason}"
    if status := _status_line(case):
        text += f"\n{status}"
    items: list[discord.ui.Item] = [
        discord.ui.Section(discord.ui.TextDisplay(text), accessory=discord.ui.Thumbnail(avatar_url or DEFAULT_AVATAR))
    ]
    context = [f"⚡ Triggered by case #{case.triggered_by}"] if case.triggered_by else []
    if case.context:
        context.append(case.context)
    if context:
        items.append(discord.ui.TextDisplay("-# " + " · ".join(context)))
    footer = f"-# ID: {case.user_id} · {_ts(case.created_at, 'f')}"
    if case.source and case.source not in ("command", "panel"):
        footer += f" · via {case.source}"
    items.append(discord.ui.TextDisplay(footer))
    if buttons:
        row = [discord.ui.Button(label="Edit reason", emoji="✏️", custom_id=f"{CASE}:edit:{case.id}")]
        if case.active and case.action in UNDO_LABELS:
            label, emoji = UNDO_LABELS[case.action]
            row.append(
                discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.success, custom_id=f"{CASE}:undo:{case.id}")
            )
        row.append(discord.ui.Button(label="History", emoji="📜", custom_id=f"{CASE}:history:{case.id}"))
        items.append(discord.ui.ActionRow(*row))
    color = CASE_STYLE.get(case.action, (None, None, discord.Color.blurple()))[2] if case.active else discord.Color.dark_grey()
    return discord.ui.DesignerView(discord.ui.Container(*items, color=color), timeout=None)


def reply_card(text: str, color: discord.Color, footer: str | None = None) -> discord.ui.DesignerView:
    """A one-block reply (mod command confirmations and friends)."""
    content = text + (f"\n-# {footer}" if footer else "")
    return discord.ui.DesignerView(discord.ui.Container(discord.ui.TextDisplay(content), color=color))


# --- automod alert cards (cogs/moderation.py builds them, cogs/mod_interactions.py collapses them) ---


def quote_block(text: str | None, limit: int = ALERT_QUOTE_MAX) -> str | None:
    """The flagged message as a "> " quote: defanged (no pings or live links), markdown escaped, trimmed."""
    text = (text or "").strip()
    if not text:
        return None
    text = discord.utils.escape_markdown(defang(text))
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return "\n".join("> " + line for line in text.splitlines() if line.strip())


def alert_card(
    head: str,
    avatar_url: str | None,
    buttons: list[discord.ui.Button],
    *,
    color: discord.Color,
    snapshot: str | None = None,
    quote: str | None = None,
    gallery: tuple[str, ...] = (),
    footer: str | None = None,
) -> discord.ui.DesignerView:
    """The expanded automod alert: head text + avatar, user snapshot, captured message, image gallery, buttons."""
    items: list[discord.ui.Item] = [
        discord.ui.Section(discord.ui.TextDisplay(head), accessory=discord.ui.Thumbnail(avatar_url or DEFAULT_AVATAR)),
        discord.ui.Separator(),
    ]
    for text in (snapshot, quote):
        if text:
            items.append(discord.ui.TextDisplay(text))
    if gallery:
        items.append(discord.ui.MediaGallery(*(discord.MediaGalleryItem(url) for url in gallery[:4])))
    if footer:
        items.append(discord.ui.TextDisplay(footer))
    items.append(discord.ui.ActionRow(*buttons, id=ALERT_ACTIONS_ID))
    return discord.ui.DesignerView(discord.ui.Container(*items, color=color), timeout=None)


def user_line(user, *facts: str) -> str:
    """'👤 **name** (`id`)' plus a small facts line; summary_text finds the name by this shape."""
    name = defang(discord.utils.escape_markdown(str(user)))
    return f"👤 **{name}** (`{user.id}`)" + (f"\n-# {' · '.join(facts)}" if facts else "")


def alert_container(source) -> discord.ui.Container | None:
    """The first Container of a DesignerView."""
    return next((item for item in source.children if isinstance(item, discord.ui.Container)), None)


def detail_dicts(container: discord.ui.Container) -> list[dict]:
    """The container's items as component dicts, minus the parts a collapse replaces."""
    return [
        data
        for item, data in zip(container.items, container.to_component_dict()["components"])
        if getattr(item, "id", None) not in ALERT_RESERVED_IDS
    ]


def handled_lines(container: discord.ui.Container) -> list[str]:
    item = container.get_item(ALERT_HANDLED_ID)
    return [line.removeprefix("-# ") for line in (item.content.split("\n") if item else []) if line.strip()]


def clone_buttons(container: discord.ui.Container, *, quick_disabled: str | None = None) -> list[discord.ui.Button]:
    """Fresh copies of the action row's quick-action buttons; the one with custom id quick_disabled, or every
    quick button when quick_disabled is "", comes back disabled. The show/hide toggle is dropped."""
    row = container.get_item(ALERT_ACTIONS_ID)
    buttons = []
    for old in row.children if row else []:
        if old.custom_id in (ALERT_SHOW, ALERT_HIDE):
            continue
        disabled = old.disabled or quick_disabled == "" or old.custom_id == quick_disabled
        buttons.append(
            discord.ui.Button(
                label=old.label, style=old.style, emoji=old.emoji, custom_id=old.custom_id, disabled=disabled
            )
        )
    return buttons


def toggle_button(expanded: bool) -> discord.ui.Button:
    label, emoji, custom_id = ("Hide details", "🔼", ALERT_HIDE) if expanded else ("Show details", "🔽", ALERT_SHOW)
    return discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.secondary, custom_id=custom_id)


def trim_lines(lines: list[str]) -> list[str]:
    while len(lines) > 1 and len("\n".join(f"-# {line}" for line in lines)) > ALERT_HANDLED_MAX:
        lines = lines[1:]  # oldest first
    return lines


def _handled_text(lines: list[str]) -> discord.ui.TextDisplay:
    return discord.ui.TextDisplay("\n".join(f"-# {line}" for line in lines)[:ALERT_HANDLED_MAX], id=ALERT_HANDLED_ID)


def summary_text(details: list[dict]) -> str:
    """'✅ **Handled** · reason · **name**' from an expanded card's component dicts."""
    texts = []
    for data in details:
        for part in data.get("components") or [data]:
            if part.get("type") == 10:
                texts.append(part.get("content", ""))
    head = texts[0].splitlines()[0] if texts else ""
    reason = head.removeprefix("### ").split(" · ", 1)[-1].strip() or "Moderation Alert"
    name = next((m[1] for t in texts if t.startswith("👤 ") and (m := re.match(r"👤 \*\*(.+?)\*\*", t))), None)
    return " · ".join(["✅ **Handled**", reason, *([f"**{name}**"] if name else [])])


def collapsed_alert(summary: str, lines: list[str], buttons: list[discord.ui.Button]) -> discord.ui.DesignerView:
    row = discord.ui.ActionRow(*buttons, toggle_button(False), id=ALERT_ACTIONS_ID)
    container = discord.ui.Container(
        discord.ui.TextDisplay(summary, id=ALERT_SUMMARY_ID), _handled_text(lines), row, color=discord.Color.dark_grey()
    )
    return discord.ui.DesignerView(container, timeout=None)


def expanded_alert(
    details: list[dict], lines: list[str], buttons: list[discord.ui.Button], *, toggle: bool = True
) -> discord.ui.DesignerView:
    """The stored details back again, with the handled lines and (toggle) a Hide button."""
    colour = discord.Color.dark_grey().value
    data = {"type": 17, "components": details, "accent_color": colour, "spoiler": False}
    view = discord.ui.DesignerView.from_dict([data], timeout=None)
    container = alert_container(view)
    container.add_item(_handled_text(lines))
    container.add_item(discord.ui.ActionRow(*buttons, *([toggle_button(True)] if toggle else []), id=ALERT_ACTIONS_ID))
    return view
