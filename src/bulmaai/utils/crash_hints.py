"""Known-issue hints for a parsed crash log: short English sentences shown next to the analysis.

The rules are learned from the log corpus (`services/log_corpus`): each one is a crash staff answered the same way
more than once. New entries get added here as new corpus zips are studied. A hint is one or two sentences, Discord
markdown at most (inline code), no links, no pings, ~300 characters.
"""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING, Callable, NamedTuple

if TYPE_CHECKING:
    from bulmaai.utils.log_parser import LogReport

MAX_HINTS = 3
MAX_HINT_LEN = 300
_MAX_ADDONS = 5

# Linkage errors: code compiled against a class, method or field that isn't there at runtime.
_RE_LINKAGE = re.compile(
    r"\b(?:NoSuchMethodError|NoSuchFieldError|NoClassDefFoundError|ClassNotFoundException"
    r"|ClassMetadataNotFoundException|AbstractMethodError|IncompatibleClassChangeError)\b"
)
_RE_MIXIN_MISS = re.compile(r"could not find any targets|failed injection check")
_RE_DMZ_CODE = re.compile(r"com[./]dragonminez[./]")
_RE_MC_LINKAGE = re.compile(r"\b(?:NoSuchMethodError|NoSuchFieldError|NoClassDefFoundError)\b")
_RE_MC_CODE = re.compile(r"net[./]minecraft[./]")
_RE_BAD_LOCATION = re.compile(r"ResourceLocationException: Non \[a-z0-9/._-\] character")
_RE_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")
_RE_MISSING_ASSET = re.compile(
    r"FileNotFoundException: dragonminez:|GeckoLibException: dragonminez:[^\n]*Unable to find"
)
# "dmzsparking-1.5.0.9.jar" -> "dmzsparking": the name before the first version-like part.
_RE_JAR_VERSION = re.compile(r"[-_ +](?=v?\d|mc\d|forge|neoforge)", re.IGNORECASE)


class Rule(NamedTuple):
    name: str
    applies: Callable[[LogReport], bool]
    text: Callable[[LogReport], str]


def _cause_texts(report: LogReport) -> tuple[str, ...]:
    """The exception chain plus the (longer) deepest cause."""
    cause = report.root_cause
    return (*cause.chain, cause.exception) if cause else ()


def _jar_base(jar: str) -> str:
    stem = jar.removesuffix(".jar")
    return _RE_JAR_VERSION.split(stem, 1)[0].lower()


def _culprit(report: LogReport) -> str | None:
    cause = report.root_cause
    if cause is None:
        return None
    return cause.culprit_mod or (cause.culprit_jar and _jar_base(cause.culprit_jar)) or None


def _dmz_version(report: LogReport, unknown: str) -> str:
    return f"DMZ {report.dragonminez_version}" if report.dragonminez_version else unknown


def _is_addon(name: str) -> bool:
    return name != "dragonminez" and ("dmz" in name or "dragonminez" in name)


def _addons(report: LogReport) -> list[str]:
    """Installed DMZ addons: mod ids naming dmz/dragonminez, else jar names doing so."""
    found = [mod_id for mod_id in report.mods if _is_addon(mod_id)]
    if not found:
        found = list(dict.fromkeys(base for jar in report.jars if _is_addon(base := _jar_base(jar))))
    return found[:_MAX_ADDONS]


# ── addon_outdated ───────────────────────────────────────────────────────────


def _addon_outdated(report: LogReport) -> bool:
    if _culprit(report) == "dragonminez":
        return False
    return any(
        (_RE_LINKAGE.search(text) or _RE_MIXIN_MISS.search(text)) and _RE_DMZ_CODE.search(text)
        for text in _cause_texts(report)
    )


def _addon_outdated_text(report: LogReport) -> str:
    version = _dmz_version(report, "this DMZ version")
    culprit = _culprit(report)
    if culprit:
        return (
            f"`{culprit}` uses DragonMineZ code that isn't in {version}: it was made for another DMZ version. "
            "Update it to the build made for your DMZ version, or remove it."
        )
    fix = "Update it to the build made for your DMZ version, or remove it."
    names = _addons(report)
    # Drop names from the end until the hint fits.
    while names:
        text = (
            f"An addon uses DragonMineZ code that isn't in {version}: it was made for another DMZ version. "
            f"Likely one of your DMZ addons: {', '.join(f'`{n}`' for n in names)}. {fix}"
        )
        if len(text) <= MAX_HINT_LEN:
            return text
        names.pop()
    return (
        f"One of your DMZ addons uses DragonMineZ code that isn't in {version}: it was made for another DMZ "
        f"version. {fix}"
    )


# ── mc_version_mismatch ──────────────────────────────────────────────────────


def _mc_version_mismatch(report: LogReport) -> bool:
    culprit = _culprit(report)
    if not culprit or culprit == "dragonminez" or _addon_outdated(report):
        return False
    return any(_RE_MC_LINKAGE.search(text) and _RE_MC_CODE.search(text) for text in _cause_texts(report))


def _mc_version_mismatch_text(report: LogReport) -> str:
    versions = " / ".join(
        f"{name} {value}"
        for name, value in (("Minecraft", report.mc_version), ("Forge", report.forge_version))
        if value
    )
    this = f"this one ({versions})" if versions else "this one"
    return (
        f"`{_culprit(report)}` was built for a different Minecraft/Forge version than {this}. "
        f"Get the {report.mc_version or '1.20.1'} Forge build of it."
    )


# ── rubidium ─────────────────────────────────────────────────────────────────


def _rubidium(report: LogReport) -> bool:
    # Embeddium bundles a "rubidium" stub mod for compatibility; only a real Rubidium jar or a list without
    # Embeddium means Rubidium itself is installed.
    if any(jar.lower().startswith("rubidium") for jar in report.jars):
        return True
    return "rubidium" in report.mods and "embeddium" not in report.mods


# ── locale_digits ────────────────────────────────────────────────────────────


def _non_ascii_digit(text: str) -> bool:
    escaped = (chr(int(m.group(1), 16)) for m in _RE_UNICODE_ESCAPE.finditer(text))
    return any(not c.isascii() and unicodedata.category(c) == "Nd" for c in (*escaped, *text))


def _locale_digits(report: LogReport) -> bool:
    return any(_RE_BAD_LOCATION.search(text) and _non_ascii_digit(text) for text in _cause_texts(report))


# ── jer_capsule_trade ────────────────────────────────────────────────────────


def _jer_capsule_trade(report: LogReport) -> bool:
    cause = report.root_cause
    if cause is None:
        return False
    texts = _cause_texts(report)
    null_trader = any("NullPointerException" in t and '"trader" is null' in t for t in texts)
    return null_trader and any("CapsuleCorpMapTrade" in s for s in (*cause.frames, *texts))


# ── rules, in priority order ─────────────────────────────────────────────────

RULES: tuple[Rule, ...] = (
    Rule(
        "sparking_super",
        lambda r: any("cannot run alongside DragonMineZ Super" in t for t in _cause_texts(r)),
        lambda r: "DMZ Sparking and DragonMineZ Super can't be installed together. Remove one of them.",
    ),
    Rule(
        "jer_capsule_trade",
        _jer_capsule_trade,
        lambda r: (
            "Just Enough Resources asks DragonMineZ's Capsule Corp map trade for offers without a trader, which "
            "crashes (a DMZ bug). Remove Just Enough Resources until it's fixed."
        ),
    ),
    Rule(
        "locale_digits",
        _locale_digits,
        lambda r: (
            "The game built a texture path with your system's number digits (Arabic-Indic, etc.), which Minecraft "
            "rejects. Set Windows' region format to English, or add `-Duser.language=en -Duser.country=US` to the "
            "launcher's Java arguments."
        ),
    ),
    Rule(
        "missing_dmz_asset",
        lambda r: any(_RE_MISSING_ASSET.search(t) for t in _cause_texts(r)),
        lambda r: (
            "An addon or resource pack points to a DragonMineZ texture/animation that "
            f"{_dmz_version(r, 'your DMZ version')} doesn't have. Update the addon or resource pack to match your "
            "DMZ version (or remove it)."
        ),
    ),
    Rule("addon_outdated", _addon_outdated, _addon_outdated_text),
    Rule("mc_version_mismatch", _mc_version_mismatch, _mc_version_mismatch_text),
    Rule(
        "rubidium",
        _rubidium,
        lambda r: (
            "Rubidium is abandoned and breaks rendering with current mods (e.g. `Pose stack not empty`, Oculus mixin "
            "failures). Replace it with Embeddium."
        ),
    ),
)


def hints_for(report: LogReport) -> list[str]:
    """The known-issue hints that apply to a parsed log, most specific first, at most MAX_HINTS, no duplicates."""
    hints: list[str] = []
    for rule in RULES:
        if len(hints) >= MAX_HINTS:
            break
        if rule.applies(report):
            text = rule.text(report)
            if text not in hints:
                hints.append(text)
    return hints
