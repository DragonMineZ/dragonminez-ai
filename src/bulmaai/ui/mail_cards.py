"""Cards for mail to the MAIL_USER mailbox (services/mail_inbox.py), posted in mail_channel_id. Pure builders: the
card is rebuilt from the mail_messages row. Button: mail:reply:<mail_id>; its form: mail_form:reply:<mail_id>. Both are
routed by custom id in cogs/mail_inbox.py. Everything from the email is untrusted: quoted, defanged, markdown escaped."""

from typing import Any

import discord

from bulmaai.services.ai_guard import defang
from bulmaai.ui.mod_cards import quote_block
from bulmaai.ui.v2 import fit, trim

BUTTON = "mail"
FORM = "mail_form"
ACTIONS = ("reply",)
FIELD_TEXT = "text"
REPLY_MAX = 4000
BODY_QUOTE = 2500
REPLY_QUOTE = 1000


def custom_id(action: str, mail_id: int) -> str:
    return f"{BUTTON}:{action}:{mail_id}"


def form_id(action: str, mail_id: int) -> str:
    return f"{FORM}:{action}:{mail_id}"


def _parse(value: str | None, prefix: str) -> tuple[str, int] | None:
    parts = (value or "").split(":")
    if len(parts) != 3 or parts[0] != prefix or parts[1] not in ACTIONS or not parts[2].isdigit():
        return None
    return parts[1], int(parts[2])


def parse_custom_id(value: str | None) -> tuple[str, int] | None:
    return _parse(value, BUTTON)


def parse_form_id(value: str | None) -> tuple[str, int] | None:
    return _parse(value, FORM)


def _ts(when, style: str = "R") -> str:
    return discord.utils.format_dt(when, style) if when else "unknown"


def _safe(text: str | None, limit: int) -> str:
    return trim(discord.utils.escape_markdown(defang(" ".join((text or "").split()))), limit)


def _head(mail: Any) -> str:
    subject = _safe(mail.subject, 200) or "(no subject)"
    sender = _safe(mail.from_addr, 200)
    if name := _safe(mail.from_name, 100):
        sender = f"{name} <{sender}>"
    return f"### 📧 {subject}\n**From** {sender}　**Received** {_ts(mail.received_at or mail.created_at)}"


def _attachments(mail: Any) -> str | None:
    names = [_safe(str(name), 100) for name in (mail.attachments or []) if str(name).strip()]
    return trim("-# 📎 " + ", ".join(names), 500) if names else None


def mail_card(mail: Any) -> discord.ui.DesignerView:
    """Open: the mail plus a Reply button. Replied: no button, who replied and what they sent."""
    body = "\n".join(filter(None, [quote_block(mail.body, BODY_QUOTE) or "> *empty*", _attachments(mail)]))
    replied = None
    if mail.replied_at:
        replied = f"↩️ Replied by <@{mail.replied_by}> {_ts(mail.replied_at)}\n" + (
            quote_block(mail.reply_text, REPLY_QUOTE) or "> *empty*"
        )
    parts = fit([_head(mail), body, replied or ""])
    items: list[discord.ui.Item] = [discord.ui.TextDisplay(parts[0]), discord.ui.Separator(), discord.ui.TextDisplay(parts[1])]
    if replied:
        items += [discord.ui.Separator(), discord.ui.TextDisplay(parts[2])]
        return discord.ui.DesignerView(discord.ui.Container(*items, color=discord.Color.green()), timeout=None)
    button = discord.ui.Button(label="↩️ Reply", style=discord.ButtonStyle.primary, custom_id=custom_id("reply", mail.id))
    container = discord.ui.Container(*items, discord.ui.ActionRow(button), color=discord.Color.blurple())
    return discord.ui.DesignerView(container, timeout=None)


def reply_modal(mail: Any, mail_user: str | None) -> discord.ui.DesignerModal:
    text = discord.ui.InputText(
        style=discord.InputTextStyle.long, custom_id=FIELD_TEXT, required=True, max_length=REPLY_MAX
    )
    return discord.ui.DesignerModal(
        discord.ui.Label("Reply", text, description=trim(f"Sent from {mail_user or 'the mailbox'}.", 100)),
        title=trim(f"Reply to {' '.join(str(mail.from_addr).split())}", 45),
        custom_id=form_id("reply", mail.id),
        store=False,
    )


def read_text(components: Any) -> str:
    """The modal's one text field, from a modal_submit payload (Label components wrap their item in "component")."""
    stack = list(components) if isinstance(components, list) else []
    while stack:
        item = stack.pop(0)
        if not isinstance(item, dict):
            continue
        if item.get("custom_id") == FIELD_TEXT:
            return str(item.get("value") or "").strip()
        if isinstance(item.get("component"), dict):
            stack.append(item["component"])
        stack.extend(item.get("components") or [])
    return ""
