"""Code-enforced guardrails around the support AI. The system prompt asks the model to behave; this is
what still holds when someone talks it out of that: what /ask will accept, and what any AI reply may
contain before it reaches Discord (allowed_mentions is the other half, see bot.SAFE_MENTIONS)."""

import re
from urllib.parse import urlsplit

from bulmaai.services.moderation import (
    DomainClassification,
    _matched_banned_word,
    classify_domain,
    detect_discord_invites,
    extract_urls,
)

ASK_MAX_QUESTION_LENGTH = 500
NO_REPLY = "(no reply)"
# Replies carrying these never reach Discord; the rest are defused and only kept out of public /ask.
BLOCKING_FLAGS = frozenset({"banned_word", "blocked_domain"})

_MASS_MENTION_RE = re.compile(r"@(everyone|here)", re.IGNORECASE)
_RAW_MENTION_RE = re.compile(r"<@[!&]?\d+>")

# ponytail: phrase list, not a classifier; it catches the copy-paste jailbreaks, the output guard and
# allowed_mentions catch whatever gets past it. Add phrases here as new ones show up in the logs.
_ASK_INJECTION_RE = re.compile(
    r"""
    \b(ignore|ignora|ignorar|disregard|forget|olvida|esque[cç]a|override|bypass)\b[^\n]{0,40}?
        \b(instructions?|instrucciones|instru[cç][oõ]es|rules|reglas|regras|prompt|guidelines|guardrails|filters?)\b
    | \b(system|developer|hidden)\s+prompt\b
    | \b(repeat|repite|repita|echo|say|di|diga|write|escribe|escreva|type)\b[^\n]{0,30}?
        \b(after\s+me|verbatim|word\s+for\s+word|textualmente|depois\s+de\s+mim|despu[eé]s\s+de\s+m[ií])
    | \b(repeat|repite|repita|echo)\b[^\n]{0,30}?\b(exactly|exactamente|exatamente|literally|literalmente)\b
    | \bjailbr[eo]ak | \bdeveloper\s+mode\b | \bdo\s+anything\s+now\b | \bpretend\s+(to\s+be|you\s+are)\b
    | \b(ping|mention|tag)\s+@?(everyone|here|everybody)\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def defuse_mentions(text: str) -> str:
    """@everyone/@here become inert text and raw <@id>/<@&id> are dropped, whatever allowed_mentions says."""
    return _MASS_MENTION_RE.sub(lambda match: "@​" + match.group(1), _RAW_MENTION_RE.sub("", text))


def screen_question(text: str) -> str | None:
    """Why a /ask question is refused before it costs a model call, or None if it may go through."""
    if len(text) > ASK_MAX_QUESTION_LENGTH:
        return "too_long"
    if _MASS_MENTION_RE.search(text) or _RAW_MENTION_RE.search(text):
        return "mention"
    if _ASK_INJECTION_RE.search(text):
        return "injection"
    return None


def screen_reply(text: str, settings: object) -> tuple[str, frozenset[str]]:
    """Defused reply plus what was found; a blocking flag replaces the reply with NO_REPLY."""
    flags: set[str] = set()
    if _MASS_MENTION_RE.search(text) or _RAW_MENTION_RE.search(text):
        flags.add("mention")
    text = defuse_mentions(text)
    if _matched_banned_word(text, tuple(getattr(settings, "moderation_banned_words", ()) or ())):
        flags.add("banned_word")
    wiki_host = urlsplit(str(getattr(settings, "wiki_base_url", "") or "")).hostname
    allowed = (*(getattr(settings, "moderation_allowed_domains", ()) or ()), *([wiki_host] if wiki_host else []))
    blocked = tuple(getattr(settings, "moderation_blocked_domains", ()) or ())
    for url in extract_urls(text):
        verdict = classify_domain(url.domain, allowed_domains=allowed, blocked_domains=blocked)
        if verdict is DomainClassification.BLOCKED:
            flags.add("blocked_domain")
        elif verdict is DomainClassification.UNKNOWN:
            flags.add("unknown_link")
    if detect_discord_invites(text):
        flags.add("invite")
    if flags & BLOCKING_FLAGS:
        return NO_REPLY, frozenset(flags)
    return text, frozenset(flags)
