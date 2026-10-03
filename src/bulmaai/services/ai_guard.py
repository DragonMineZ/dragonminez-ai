"""Code-enforced guardrails around the support AI. The system prompt asks the model to behave; this is
what still holds when someone talks it out of that: what /ask will accept, what goes into the model as
"who said what", and what any AI reply may contain before it reaches Discord (allowed_mentions is the
other half, see bot.SAFE_MENTIONS)."""

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
# Replies carrying these never reach Discord; the rest are defused and only kept out of public channels.
BLOCKING_FLAGS = frozenset({"banned_word", "blocked_domain"})
PUBLIC_KINDS = frozenset({"answer", "clarify"})
# What the AI may link to: our own pages, not whole hosts (anyone can publish on github.com or curseforge.com).
# Matched as host+path prefixes ending at a "/" boundary; the configured wiki host is allowed on top.
AI_LINK_PREFIXES = (
    "github.com/dragonminez",
    "curseforge.com/minecraft/mc-mods/dragonminez",
    "modrinth.com/mod/dragonminez",
    "patreon.com/dragonminez",
)

_MASS_MENTION_RE = re.compile(r"@(everyone|here)", re.IGNORECASE)
_RAW_MENTION_RE = re.compile(r"<@[!&]?\d+>")
# Catches IP-literal and non-ASCII hosts too, which moderation.URL_RE skips.
_SCHEME_URL_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>()\[\]\"'`]+")
_MASKED_LINK_RE = re.compile(r"\[([^\]\n]{1,200})\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
# A transcript speaker label typed inside a message, e.g. "\n[staff Bruno · just now]".
_FORGED_LABEL_RE = re.compile(r"(?im)^(\s*)\[(?=\s*(?:requester|staff|participant|assistant|system|developer)\b)")
_NAME_JUNK_RE = re.compile(r"[\[\]()<>@#:·\r\n`*_~|]+")

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


def safe_name(name: object) -> str:
    """A display name the model can't read as a label, role or instruction: "x (roles: staff)" -> "x roles staff"."""
    return " ".join(_NAME_JUNK_RE.sub(" ", str(name or "")).split())[:40] or "unknown"


def neutralize_labels(text: str) -> str:
    """Message text can't open a fake speaker label; the real labels are added by openai_client only."""
    return _FORGED_LABEL_RE.sub(r"\1(quoted) [", text)


def strip_links(text: str) -> str:
    """For text that outlives the conversation (AI knowledge): keep masked-link labels, drop every URL."""
    return _SCHEME_URL_RE.sub("[link removed]", _MASKED_LINK_RE.sub(r"\1", text))


def _link_is_ours(url: str, wiki_host: str | None) -> bool:
    parts = urlsplit(url if "://" in url else f"//{url}")
    host = (parts.hostname or "").removeprefix("www.")
    if wiki_host and host == wiki_host.removeprefix("www."):
        return True
    target = f"{host}{parts.path}".rstrip("/").lower()
    return any(target == prefix or target.startswith(prefix + "/") for prefix in AI_LINK_PREFIXES)


def can_post_publicly(result: dict) -> bool:
    """Only clean, on-topic answers go public; anything the guard touched or the model flagged stays private."""
    return result.get("kind") in PUBLIC_KINDS and not result.get("guard_flags")


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
    blocked = tuple(getattr(settings, "moderation_blocked_domains", ()) or ())

    def unmask(match: re.Match) -> str:
        # Masked links hide where they go; only our own pages keep their label.
        return match.group(0) if _link_is_ours(match.group(2), wiki_host) else f"{match.group(1)} (<{match.group(2)}>)"

    text = _MASKED_LINK_RE.sub(unmask, text)
    urls = {match.group(0) for match in _SCHEME_URL_RE.finditer(text)} | {url.normalized for url in extract_urls(text)}
    for url in urls:
        host = (urlsplit(url if "://" in url else f"//{url}").hostname or "").lower()
        if classify_domain(host, blocked_domains=blocked) is DomainClassification.BLOCKED:
            flags.add("blocked_domain")
        elif not _link_is_ours(url, wiki_host):
            flags.add("unknown_link")
    if detect_discord_invites(text):
        flags.add("invite")
    if flags & BLOCKING_FLAGS:
        return NO_REPLY, frozenset(flags)
    return text, frozenset(flags)
