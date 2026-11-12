"""Staff copilot in tickets: what the welcome card's "📋 Summary" button (ticket_views.control_row) shows, the readback
of staff-signed cards, and the card that translates a staff message.

The summary is an ephemeral card for the staff member who clicked, labelled in their language (TEXT; the AI's summary
itself is English). A reply posted through Staff Bulma's ticket_reply (or the removed Draft reply) is a public card of the text plus a
"-# ✍️ <@staff>" line (SIGNED_TEXT_ID); history and transcripts still read it as that staff member's turn.
A translation (TRANSLATION_TEXT_ID) is in the ticket's language, posted through the ticket's webhook as the person
who wrote the message (or, failing that, as Bulma's reply to it); history and transcripts skip it, since it repeats
that message. An owner's message is translated for the staff in the ticket instead: one language looks the same,
several share one card (translations_card), each under its language's label, in the same TEXT_LIMIT.

Pure helpers only (cards, readback); the Discord wiring is in cogs/tickets.py (buttons, gates,
posting) and cogs/ai_tickets.py (the AI work, history and transcripts)."""

import re
from typing import Any

import discord

from bulmaai.ui.ticket_views import LANGUAGES as LANGUAGE_LABELS
from bulmaai.ui.v2 import TEXT_LIMIT, card, fit, trim
from bulmaai.utils.i18n import t, table

COLOR = discord.Color.from_rgb(88, 101, 242)
# Component ids of the texts that matter: the model can't set these, so a card's own text can't pose as one.
SIGNED_TEXT_ID = 821  # the signature line of a staff reply posted by Staff Bulma (and by the removed Draft reply)
TRANSLATION_TEXT_ID = 822  # a translation card's text
ORIGINAL_TEXT_ID = 823  # the quoted original on a translation card
ORIGINAL_LIMIT = 1000  # the quoted original on a translation card
TRANSLATED_BY_RE = re.compile(r"^-# 🌐 .* · <@!?(\d+)>$")  # the footer of a card standing in for its original
# A posted reply's text; the signature line fits in what's left of the message's 4000 characters.
POST_LIMIT = TEXT_LIMIT
SIGNATURE_RE = re.compile(r"^-# ✍️ <@!?(\d+)>$")
SUMMARY_FIELDS = ("problem", "tried", "current_state", "next_step")
TEXT = table({
    "en": {
        "problem": "Problem",
        "tried": "Tried",
        "current_state": "Current state",
        "next_step": "Next step",
        "so_far": "Ticket #{number} so far",
    },
    "es": {
        "problem": "Problema",
        "tried": "Intentado",
        "current_state": "Estado actual",
        "next_step": "Siguiente paso",
        "so_far": "Ticket #{number} hasta ahora",
    },
    "pt": {
        "problem": "Problema",
        "tried": "Tentado",
        "current_state": "Estado atual",
        "next_step": "Próximo passo",
        "so_far": "Ticket #{number} até agora",
    },
})
# "-# Translated from {source}", in the ticket's language.
TRANSLATED_FROM: dict[str, dict[str, str]] = table({
    "en": {"en": "Translated from English", "es": "Translated from Spanish", "pt": "Translated from Portuguese"},
    "es": {"en": "Traducido del inglés", "es": "Traducido del español", "pt": "Traducido del portugués"},
    "pt": {"en": "Traduzido do inglês", "es": "Traduzido do espanhol", "pt": "Traduzido do português"},
})


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


def summary_card(number: int, summary: Any, language: str = "en") -> discord.ui.DesignerView:
    """The ticket so far (services/staff_copilot.TicketSoFar), for the staff member who asked."""
    fields = [f"**{t(TEXT, key, language)}**\n{getattr(summary, key)}" for key in SUMMARY_FIELDS]
    title = t(TEXT, "so_far", language, number=f"{number:04d}")
    return card("\n".join(fit([f"### 📋 {title}", *fields])), color=COLOR)


def signature_line(staff_id: int) -> str:
    return f"-# ✍️ <@{staff_id}>"


def posted_card(text: str, staff_id: int) -> discord.ui.DesignerView:
    """A staff reply posted by Staff Bulma's ticket_reply: the text, then their signature (sent without pings)."""
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


def quote(text: str) -> str:
    """text (already mention-free, e.g. clean_content) as a quote block, cut to ORIGINAL_LIMIT."""
    return "\n".join(f"> {line}" for line in trim(text, ORIGINAL_LIMIT).splitlines())


def _footer(source: str, language: str, author_id: int | None) -> str:
    """"-# 🌐 Translated from {source}", plus " · <@author>" (never pings: sent with no mentions allowed) on a card that
    stands in for its deleted original (translated_original)."""
    by = f" · <@{author_id}>" if author_id else ""
    return f"-# 🌐 {translated_from(source, language)}{by}"


def _translation_view(head: str, body: str, footer: str) -> discord.ui.DesignerView:
    items: list[discord.ui.Item] = [discord.ui.TextDisplay(head, id=ORIGINAL_TEXT_ID), discord.ui.Separator()] if head else []
    items += [discord.ui.TextDisplay(body, id=TRANSLATION_TEXT_ID), discord.ui.TextDisplay(footer)]
    return discord.ui.DesignerView(discord.ui.Container(*items, color=COLOR), timeout=None)


def translation_card(
    translation: str, source: str, language: str, original: str | None = None, author_id: int | None = None
) -> discord.ui.DesignerView:
    """A message (in source) in the ticket's language. Sent through the ticket's webhook as its author, it quotes the
    original above a separator; as a reply (the fallback) it has only the translation. "-# 🌐 Translated from {source}"
    closes it."""
    footer = _footer(source, language, author_id)
    head = quote(original) if original and original.strip() else ""
    return _translation_view(head, trim(translation, TEXT_LIMIT - len(head) - len(footer)), footer)


def translation_label(language: str) -> str:
    flag, name = LANGUAGE_LABELS[language]
    return f"-# {flag} {name}"


def _share(parts: list[str], room: int) -> list[str]:
    """parts cut to fit room together, fairly: the short ones stay whole and the long ones split what's left evenly."""
    out = list(parts)
    left = max(room, 0)
    order = sorted(range(len(parts)), key=lambda i: len(parts[i]))
    for done, i in enumerate(order):
        out[i] = trim(parts[i], left // (len(parts) - done))
        left -= len(out[i])
    return out


def translations_card(
    translations: dict[str, str], source: str, original: str | None = None, author_id: int | None = None
) -> discord.ui.DesignerView:
    """A ticket owner's message (in source) for the staff: one translation is translation_card; several share one
    text (TRANSLATION_TEXT_ID), each under its language's label ("-# 🇬🇧 English"), the long ones sharing the room
    evenly (_share), and the footer is English, the staff's shared language."""
    if len(translations) == 1:
        (language, translation), = translations.items()
        return translation_card(translation, source, language, original=original, author_id=author_id)
    footer = _footer(source, "en", author_id)
    head = quote(original) if original and original.strip() else ""
    labels = [translation_label(language) for language in translations]
    room = TEXT_LIMIT - len(head) - len(footer) - sum(len(label) + 3 for label in labels)
    bodies = _share([text.strip() for text in translations.values()], room)
    body = "\n\n".join(f"{label}\n{text}" for label, text in zip(labels, bodies))
    return _translation_view(head, body, footer)


def is_translation(message: Any) -> bool:
    return any(component_id == TRANSLATION_TEXT_ID for component_id, _text in _texts(message))


def translated_original(message: Any) -> tuple[int, str] | None:
    """(author id, original text) of a translation card that stands in for its deleted original; None for any
    other card, the reply fallback included."""
    texts = _texts(message)
    if not any(component_id == TRANSLATION_TEXT_ID for component_id, _text in texts):
        return None
    author = next((m for component_id, text in texts if (m := TRANSLATED_BY_RE.search(text))), None)
    original = next((text for component_id, text in texts if component_id == ORIGINAL_TEXT_ID), None)
    if author is None or original is None:
        return None
    return int(author[1]), "\n".join(line.removeprefix(">").removeprefix(" ") for line in original.splitlines())
