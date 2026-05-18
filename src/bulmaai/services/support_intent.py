import re
from typing import Literal

SUPPORT_INTENT_SUPPORT_QUESTION = "support_question"
SUPPORT_INTENT_PATREON_WHITELIST = "patreon_whitelist"
SUPPORT_INTENT_UNCLEAR = "unclear"

SupportIntent = Literal["support_question", "patreon_whitelist", "unclear"]

_PATREON_WHITELIST_PATTERNS = (
    r"\bpatreon\b",
    r"\bwhitelist\b",
    r"\ballowlist\b",
    r"\bwhite[- ]?list\b",
    r"\baccess\b",
    r"\bacesso\b",
    r"\bacceso\b",
    r"\bbeta\b",
    r"\balpha\b",
    r"\bearly[- ]?access\b",
    r"\bcreator\b",
)

_SUPPORT_KEYWORDS = (
    "dragonminez",
    "dmz",
    "mod",
    "minecraft",
    "forge",
    "server",
    "client",
    "install",
    "installation",
    "configure",
    "config",
    "version",
    "update",
    "download",
    "curseforge",
    "github",
    "crash",
    "crashes",
    "crashing",
    "error",
    "bug",
    "broken",
    "issue",
    "problem",
    "doesn't work",
    "doesnt work",
    "does not work",
    "not working",
    "log",
    "launcher",
    "modpack",
    "addon",
    "transform",
    "transformation",
    "form",
    "forms",
    "race",
    "saiyan",
    "namekian",
    "arcosian",
    "majin",
    "ki",
    "stats",
    "attributes",
    "skill",
    "skills",
    "quest",
    "quests",
    "npc",
    "dragonball",
    "dragonballs",
    "wish",
    "wishes",
    "space pod",
    "patreon",
    "whitelist",
    "allowlist",
    "beta",
    "access",
    "instalar",
    "configurar",
    "servidor",
    "cliente",
    "crasheo",
    "crashea",
    "error",
    "problema",
    "no funciona",
    "transformarme",
    "transformacion",
    "habilidad",
    "mision",
    "misiones",
    "dragones",
    "esferas",
    "instalar",
    "configurar",
    "servidor",
    "cliente",
    "travando",
    "erro",
    "problema",
    "nao funciona",
    "não funciona",
    "transformar",
    "transformacao",
    "habilidade",
    "missao",
    "missoes",
)

_QUESTION_HINT_RE = re.compile(r"(?i)\b(how|why|what|where|when|can|can't|cannot|does|do|is|are|help)\b")


def _normalized_text(text: str) -> str:
    return " ".join(text.lower().split())


def _has_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _looks_like_beta_access_request(text: str) -> bool:
    if not text:
        return False
    has_patreon_or_beta = bool(re.search(r"(?i)\b(patreon|beta|alpha|early[- ]?access|creator)\b", text))
    has_access_or_list = bool(
        re.search(r"(?i)\b(whitelist|allowlist|white[- ]?list|access|acceso|acesso|entrar|join)\b", text)
    )
    if has_patreon_or_beta and has_access_or_list:
        return True

    matches = sum(1 for pattern in _PATREON_WHITELIST_PATTERNS if re.search(pattern, text, re.IGNORECASE))
    return matches >= 3 and ("patreon" in text or "beta" in text or "alpha" in text)


def classify_support_intent(text: str, *, has_image: bool = False) -> SupportIntent:
    normalized = _normalized_text(text)
    if _looks_like_beta_access_request(normalized):
        return SUPPORT_INTENT_PATREON_WHITELIST
    if has_image and normalized:
        return SUPPORT_INTENT_SUPPORT_QUESTION
    if not normalized:
        return SUPPORT_INTENT_UNCLEAR
    if _has_keyword(normalized, _SUPPORT_KEYWORDS):
        return SUPPORT_INTENT_SUPPORT_QUESTION
    if _QUESTION_HINT_RE.search(normalized) and _has_keyword(
        normalized,
        ("help", "support", "ticket", "crash", "error", "install", "config", "server", "mod"),
    ):
        return SUPPORT_INTENT_SUPPORT_QUESTION
    return SUPPORT_INTENT_UNCLEAR
