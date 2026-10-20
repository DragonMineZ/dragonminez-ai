"""👍/👎 under Bulma's answers. The last card of a plain answer (not a handoff or a question, no controls, not in a
ticket, where the "Have I solved your problem?" prompt and staff cover it) gets a row of two emoji buttons for the
requester; their first click is stored (services/ai_feedback) and the card swaps the row for a small thanks line,
which history and transcripts skip (ai_tickets._card_text). The weekly support report counts the ratings.

Pure helpers only (custom ids, the row, the texts); the Discord wiring is in cogs/ai_tickets.py."""

import discord

CUSTOM_ID_PREFIX = "ai_fb:"  # ai_fb:{requester_id}:{up|down}
ANSWER_KINDS = (None, "answer")  # what the model marked (None: no marker); handoffs, questions and off-topic get none

TEXT: dict[str, dict[str, str]] = {
    "en": {
        "thanks": "-# Thanks for the feedback!",
        "not_yours": "Only the person Bulma answered can rate this.",
    },
    "es": {
        "thanks": "-# ¡Gracias por tu opinión!",
        "not_yours": "Solo la persona a la que Bulma respondió puede valorar esto.",
    },
    "pt": {
        "thanks": "-# Obrigado pelo feedback!",
        "not_yours": "Só a pessoa a quem a Bulma respondeu pode avaliar isto.",
    },
}
# Every language's thanks line: a card text that is exactly one of these is ours, never the answer.
THANKS_LINES = frozenset(texts["thanks"] for texts in TEXT.values())


def text(language: str | None, key: str) -> str:
    return TEXT.get(language or "en", TEXT["en"])[key]


def wanted(settings, kind: str | None) -> bool:
    """Whether an answer of this kind gets the buttons (the caller adds where: never in tickets, never with controls,
    never on our fallback texts)."""
    return bool(getattr(settings, "ai_answer_feedback_enabled", False)) and kind in ANSWER_KINDS


def custom_id(requester_id: int, helpful: bool) -> str:
    return f"{CUSTOM_ID_PREFIX}{requester_id}:{'up' if helpful else 'down'}"


def parse_custom_id(value: str) -> tuple[int, bool] | None:
    """(requester id, helpful) of a feedback button."""
    if not value.startswith(CUSTOM_ID_PREFIX):
        return None
    try:
        requester_id, vote = value.removeprefix(CUSTOM_ID_PREFIX).split(":")
        return (int(requester_id), vote == "up") if vote in ("up", "down") else None
    except ValueError:
        return None


def feedback_row(requester_id: int) -> discord.ui.ActionRow:
    """The two buttons, emoji only, routed by custom id in the cog's on_interaction (they survive restarts)."""
    return discord.ui.ActionRow(
        *(
            discord.ui.Button(
                emoji=emoji, style=discord.ButtonStyle.secondary, custom_id=custom_id(requester_id, helpful)
            )
            for emoji, helpful in (("👍", True), ("👎", False))
        )
    )
