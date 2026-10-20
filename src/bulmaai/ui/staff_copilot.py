"""Staff copilot in tickets: what the welcome card's "📋 Summary" and "✍️ Draft reply" buttons (ticket_views.control_row)
show, the staff-signed card a draft becomes when it is posted, and the card that translates a staff message.

Summary and the draft are ephemeral cards for the staff member who clicked (English). The draft's text lives on its
own card (DRAFT_TEXT_ID), so Post and Edit & post read it back from there: no DB row. A posted draft is a public card
of the text plus a "-# ✍️ <@staff>" line (SIGNED_TEXT_ID); history and transcripts read it as that staff member's turn.
A translation (TRANSLATION_TEXT_ID) is in the ticket's language and replies to the staff message it translates;
history and transcripts skip it, since it repeats that message.

Pure helpers only (custom ids, cards, the modal, readback); the Discord wiring is in cogs/tickets.py (buttons, gates,
posting) and cogs/ai_tickets.py (the AI work, history and transcripts)."""

import re
from typing import Any

import discord

from bulmaai.ui.v2 import TEXT_LIMIT, card, fit, trim

COLOR = discord.Color.from_rgb(88, 101, 242)
DRAFT_PREFIX = "staff_draft:"  # staff_draft:{post|edit|discard}, the buttons on the ephemeral draft card
DRAFT_POST, DRAFT_EDIT, DRAFT_DISCARD = "post", "edit", "discard"
EDIT_MODAL_PREFIX = "staff_draft_edit:"  # staff_draft_edit:{ticket channel id}
EDIT_FIELD_ID = "text"
# Component ids of the texts that matter: the model can't set these, so a card's own text can't pose as one.
DRAFT_TEXT_ID = 820  # the draft on its ephemeral card
SIGNED_TEXT_ID = 821  # the signature line of a posted draft
TRANSLATION_TEXT_ID = 822  # a translation card's text
# A posted reply's text; the signature line fits in what's left of the message's 4000 characters.
POST_LIMIT = TEXT_LIMIT
SIGNATURE_RE = re.compile(r"^-# ✍️ <@!?(\d+)>$")
POSTED = "Posted."
DISCARDED = "Discarded."
SUMMARY_FIELDS = (
    ("Problem", "problem"),
    ("Tried", "tried"),
    ("Current state", "current_state"),
    ("Next step", "next_step"),
)
# "-# Translated from {source}", in the ticket's language.
TRANSLATED_FROM: dict[str, dict[str, str]] = {
    "en": {"en": "Translated from English", "es": "Translated from Spanish", "pt": "Translated from Portuguese"},
    "es": {"en": "Traducido del inglés", "es": "Traducido del español", "pt": "Traducido del portugués"},
    "pt": {"en": "Traduzido do inglês", "es": "Traduzido do espanhol", "pt": "Traduzido do português"},
}


def _texts(message: Any) -> list[tuple[int | None, str]]:
    """(component id, text) of every text block on a V2 message, nested ones included."""
    out: list[tuple[int | None, str]] = []

    def walk(items) -> None:
        for item in items or ():
            if item.get("type") == 10 and item.get("content"):
                out.append((item.get("id"), item["content"]))
            walk(item.get("components"))

    walk([component.to_dict() for component in getattr(message, "components", None) or ()])
    return out


def summary_card(number: int, summary: Any) -> discord.ui.DesignerView:
    """The ticket so far (services/staff_copilot.TicketSoFar), for the staff member who asked."""
    fields = [f"**{label}**\n{getattr(summary, key)}" for label, key in SUMMARY_FIELDS]
    return card("\n".join(fit([f"### 📋 Ticket #{number:04d} so far", *fields])), color=COLOR)


def draft_card(text: str) -> discord.ui.DesignerView:
    """A draft of the next reply to the owner, with Post / Edit & post / Discard. Only its viewer sees it."""
    buttons = [
        discord.ui.Button(label="Post", style=discord.ButtonStyle.primary, custom_id=f"{DRAFT_PREFIX}{DRAFT_POST}"),
        discord.ui.Button(label="Edit & post", style=discord.ButtonStyle.secondary, custom_id=f"{DRAFT_PREFIX}{DRAFT_EDIT}"),
        discord.ui.Button(label="Discard", style=discord.ButtonStyle.secondary, custom_id=f"{DRAFT_PREFIX}{DRAFT_DISCARD}"),
    ]
    return discord.ui.DesignerView(
        discord.ui.Container(
            discord.ui.TextDisplay(trim(text, POST_LIMIT), id=DRAFT_TEXT_ID), discord.ui.ActionRow(*buttons), color=COLOR
        ),
        timeout=None,
    )


def done_card(text: str) -> discord.ui.DesignerView:
    """What the draft card turns into: POSTED or DISCARDED."""
    return card(text, color=COLOR)


def parse_draft_custom_id(value: str) -> str | None:
    """post, edit or discard, for a draft card's button."""
    action = value.removeprefix(DRAFT_PREFIX) if value.startswith(DRAFT_PREFIX) else None
    return action if action in (DRAFT_POST, DRAFT_EDIT, DRAFT_DISCARD) else None


def draft_text(message: Any) -> str:
    """The draft on a draft card ("" when it has none)."""
    return next((text for component_id, text in _texts(message) if component_id == DRAFT_TEXT_ID), "").strip()


def edit_modal(text: str, channel_id: int) -> discord.ui.DesignerModal:
    """Edit & post: the draft in a long text field. Routed by custom id (store=False), so a restart loses nothing."""
    field = discord.ui.InputText(
        style=discord.InputTextStyle.long,
        custom_id=EDIT_FIELD_ID,
        value=trim(text, POST_LIMIT) or None,
        max_length=POST_LIMIT,
        required=True,
    )
    return discord.ui.DesignerModal(
        discord.ui.Label("Reply", field), title="Edit & post", custom_id=f"{EDIT_MODAL_PREFIX}{channel_id}", store=False
    )


def parse_edit_modal_id(value: str) -> int | None:
    """The ticket channel an Edit & post form was opened in."""
    if not value.startswith(EDIT_MODAL_PREFIX):
        return None
    try:
        return int(value.removeprefix(EDIT_MODAL_PREFIX))
    except ValueError:
        return None


def edited_text(components: Any) -> str:
    """The text submitted in the Edit & post form (Label components wrap their field in "component")."""

    def find(items: Any) -> str | None:
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("custom_id") == EDIT_FIELD_ID:
                return str(item.get("value") or "")
            nested = [item["component"]] if isinstance(item.get("component"), dict) else item.get("components")
            if (found := find(nested)) is not None:
                return found
        return None

    return (find(components) or "").strip()


def signature_line(staff_id: int) -> str:
    return f"-# ✍️ <@{staff_id}>"


def posted_card(text: str, staff_id: int) -> discord.ui.DesignerView:
    """A staff reply posted from Draft reply: the text, then their signature (sent without pings)."""
    return discord.ui.DesignerView(
        discord.ui.Container(
            discord.ui.TextDisplay(trim(text, POST_LIMIT)),
            discord.ui.TextDisplay(signature_line(staff_id), id=SIGNED_TEXT_ID),
            color=COLOR,
        ),
        timeout=None,
    )


def signed_by(message: Any) -> int | None:
    """The staff member who posted this card from Draft reply; None for any other card."""
    for component_id, text in _texts(message):
        if component_id == SIGNED_TEXT_ID and (match := SIGNATURE_RE.match(text)):
            return int(match[1])
    return None


def translated_from(source: str, language: str) -> str:
    return TRANSLATED_FROM.get(language, TRANSLATED_FROM["en"])[source]


def translation_card(translation: str, source: str, language: str) -> discord.ui.DesignerView:
    """A staff message (in source) in the ticket's language: "🌐 {translation}" and "-# Translated from {source}"."""
    footer = f"-# {translated_from(source, language)}"
    body = trim(translation, TEXT_LIMIT - len(footer) - 4)
    return discord.ui.DesignerView(
        discord.ui.Container(discord.ui.TextDisplay(f"🌐 {body}\n{footer}", id=TRANSLATION_TEXT_ID), color=COLOR),
        timeout=None,
    )


def is_translation(message: Any) -> bool:
    return any(component_id == TRANSLATION_TEXT_ID for component_id, _text in _texts(message))
