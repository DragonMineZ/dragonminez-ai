import json
import logging
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field, replace
from enum import Enum
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
ZERO_WIDTH_CHARS = "\u200b\u200c\u200d\ufeff"
URL_RE = re.compile(
    r"(?i)\b(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s<>()\[\]{}]*)?"
)
DISCORD_INVITE_DOMAINS = {
    "discord.gg",
    "discord.com",
    "www.discord.com",
    "discordapp.com",
    "www.discordapp.com",
}
DEFAULT_SUSPICIOUS_SHORTENERS = (
    "bit.ly",
    "tinyurl.com",
    "t.co",
    "goo.gl",
    "is.gd",
    "ow.ly",
    "cutt.ly",
    "rebrand.ly",
    "shorturl.at",
)


class DomainClassification(str, Enum):
    ALLOWED = "allowed"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class ModerationAction(str, Enum):
    ALLOW = "allow"
    ALERT = "alert"
    DELETE = "delete"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class UrlMatch:
    raw: str
    normalized: str
    domain: str


@dataclass(frozen=True)
class DiscordInvite:
    domain: str
    code: str


@dataclass(frozen=True)
class AttachmentMetadata:
    filename: str
    content_type: str | None
    url: str | None
    size: int | None
    width: int | None
    height: int | None
    extension: str
    is_image: bool


@dataclass(frozen=True)
class AttachmentInfo:
    filename: str
    content_type: str | None = None
    url: str | None = None
    size: int | None = None
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class MessageSignal:
    guild_id: int
    channel_id: int
    author_id: int
    content: str
    attachments: tuple[AttachmentInfo, ...] = ()
    # Unique user + role mentions (cog fills from raw_mentions/raw_role_mentions).
    mention_count: int = 0
    can_mention_everyone: bool = False


@dataclass(frozen=True)
class ModerationConfig:
    blocked_domains: tuple[str, ...] = ()
    allowed_domains: tuple[str, ...] = ()
    block_discord_invites: bool = False
    suspicious_shortener_domains: tuple[str, ...] = DEFAULT_SUSPICIOUS_SHORTENERS
    image_burst_count: int = 3
    image_burst_window_seconds: int = 20
    # Images spread across fewer messages than this never trigger the burst,
    # so a single legitimate multi-screenshot message is not punished.
    image_burst_min_messages: int = 2
    link_burst_count: int = 5
    link_burst_window_seconds: int = 60
    # Extra automod filters; 0 turns a count/limit off.
    banned_words: tuple[str, ...] = ()
    mass_mention_limit: int = 0
    block_everyone_ping: bool = False
    duplicate_count: int = 0
    duplicate_window_seconds: int = 30
    fast_message_count: int = 0
    fast_message_window_seconds: int = 8
    caps_percent: int = 70
    caps_min_length: int = 20
    emoji_limit: int = 0
    newline_limit: int = 0
    zalgo_enabled: bool = False


# Panel on/off switch per filter (moderation_disabled_filters): turning one off neutralizes its
# config but keeps its thresholds for when it is turned back on.
FILTER_OFF: dict[str, dict[str, Any]] = {
    "blocked_domain": {"blocked_domains": ()},
    "discord_invite": {"block_discord_invites": False},
    "banned_word": {"banned_words": ()},
    "mass_mention": {"mass_mention_limit": 0},
    "everyone_ping": {"block_everyone_ping": False},
    "excessive_caps": {"caps_percent": 0},
    "excessive_emoji": {"emoji_limit": 0},
    "wall_of_text": {"newline_limit": 0},
    "zalgo": {"zalgo_enabled": False},
    "duplicate_spam": {"duplicate_count": 0},
    "fast_messages": {"fast_message_count": 0},
    "link_burst": {"link_burst_count": 0},
    "suspicious_shortener": {"suspicious_shortener_domains": ()},
    "image_burst": {"image_burst_count": 0},
}


def without_filters(config: "ModerationConfig", disabled: "tuple[str, ...] | list[str]") -> "ModerationConfig":
    changes: dict[str, Any] = {}
    for name in disabled:
        changes.update(FILTER_OFF.get(name, {}))
    return replace(config, **changes) if changes else config

# Per-filter rules from moderation_filter_rules (JSON written by the panel's Automod page):
# {"discord_invite": {"action": "alert", "channels": ["123"], "roles": ["456"]}}.
# channels/roles exempt that one filter; action replaces what the filter does on a hit.
RULE_ACTIONS = ("alert", "delete", "warn", "timeout")
# Decision reasons that differ from their filter id.
_REASON_FILTER = {"link burst": "link_burst", "image burst": "image_burst"}


@dataclass(frozen=True)
class FilterRule:
    action: str = ""
    channels: frozenset[int] = frozenset()
    roles: frozenset[int] = frozenset()


def _ids(values: Any) -> frozenset[int]:
    return frozenset(int(value) for value in values or () if str(value).strip().isdigit())


@lru_cache(maxsize=8)
def parse_filter_rules(raw: str) -> dict[str, FilterRule]:
    """Lenient: a malformed value or entry is skipped (and logged once), never breaks automod."""
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
    except ValueError as error:
        log.warning("Ignoring invalid moderation_filter_rules: %s", error)
        return {}
    rules = {}
    for filter_id, entry in data.items():
        if not isinstance(entry, dict):
            continue
        action = entry.get("action") if entry.get("action") in RULE_ACTIONS else ""
        rules[str(filter_id)] = FilterRule(action, _ids(entry.get("channels")), _ids(entry.get("roles")))
    return rules


def exempt_filters(rules: dict[str, FilterRule], place_ids: "set[int]", role_ids: "set[int]") -> tuple[str, ...]:
    """Filters this message skips: its channel (or thread parent / category) or one of the author's roles is listed."""
    return tuple(
        filter_id for filter_id, rule in rules.items() if rule.channels & place_ids or rule.roles & role_ids
    )


def filter_id_for(reason: str) -> str:
    return _REASON_FILTER.get(reason, reason)


def apply_rule_action(
    decision: "ModerationDecision", rules: dict[str, FilterRule], *, timeout_seconds: int
) -> "ModerationDecision":
    if decision.action is ModerationAction.ALLOW:
        return decision
    rule = rules.get(filter_id_for(decision.reason))
    if rule is None or not rule.action:
        return decision
    if rule.action == "alert":
        return replace(decision, action=ModerationAction.ALERT, warn=False)
    if rule.action == "timeout":
        return replace(decision, action=ModerationAction.TIMEOUT, warn=False, timeout_seconds=timeout_seconds, purge=False)
    return replace(decision, action=ModerationAction.DELETE, warn=rule.action == "warn", purge=False)


@dataclass(frozen=True)
class ModerationDecision:
    action: ModerationAction
    reason: str
    details: str = ""
    source: str = ""
    domains: tuple[str, ...] = ()
    defanged_domains: tuple[str, ...] = ()
    invites: tuple[DiscordInvite, ...] = ()
    image_count: int = 0
    # None = use the caller's default timeout length (the long spam-bot one).
    timeout_seconds: int | None = None
    # Set for a scam_image match: the scam_image_hashes row it matched, for automod_hits.
    scam_hash_id: int | None = None
    # Set by a panel rule action: whether to add a warn strike (None = the filter's default).
    warn: bool | None = None
    # False: a timeout doesn't purge the author's recent messages (rule timeouts aren't spam-bot shaped).
    purge: bool = True

    @classmethod
    def allow(cls, reason: str = "allowed") -> "ModerationDecision":
        return cls(action=ModerationAction.ALLOW, reason=reason)


@dataclass
class ModerationState:
    image_events: dict[tuple[int, int], list[float]] = field(default_factory=lambda: defaultdict(list))
    image_message_events: dict[tuple[int, int], list[float]] = field(default_factory=lambda: defaultdict(list))
    link_events: dict[tuple[int, int], list[float]] = field(default_factory=lambda: defaultdict(list))
    fast_message_events: dict[tuple[int, int], list[float]] = field(default_factory=lambda: defaultdict(list))
    # (guild_id, author_id) -> [(posted_at, normalized_content, channel_id)] for duplicate_spam.
    duplicate_events: dict[tuple[int, int], list[tuple[float, str, int]]] = field(
        default_factory=lambda: defaultdict(list)
    )

    def record(
        self,
        bucket: dict[tuple[int, int], list[float]],
        key: tuple[int, int],
        now: float,
        window_seconds: float,
        *,
        count: int = 1,
    ) -> tuple[float, ...]:
        events = [event_time for event_time in bucket[key] if now - event_time <= window_seconds]
        events.extend([now] * count)
        bucket[key] = events
        return tuple(events)


def _strip_zero_width(text: str) -> str:
    return text.translate({ord(char): None for char in ZERO_WIDTH_CHARS})


def _normalize_obfuscated_text(text: str) -> str:
    value = _strip_zero_width(text)
    value = re.sub(r"(?i)\bhxxps://", "https://", value)
    value = re.sub(r"(?i)\bhxxp://", "http://", value)
    value = re.sub(r"(?i)\s*(?:\[dot]|\(dot\)|\[\.\])\s*", ".", value)
    return value


def _domain_matches(domain: str, configured_domain: str) -> bool:
    normalized_domain = domain.lower().strip(".")
    normalized_config = configured_domain.lower().strip(".")
    return normalized_domain == normalized_config or normalized_domain.endswith(f".{normalized_config}")


def _split_url(value: str) -> tuple[str, str] | None:
    target = value if "://" in value else f"//{value}"
    parsed = urlsplit(target)
    domain = (parsed.netloc or "").lower().strip(".")
    if not domain:
        return None
    if "@" in domain:
        domain = domain.rsplit("@", 1)[-1]
    if ":" in domain:
        domain = domain.split(":", 1)[0]

    normalized = value
    if parsed.scheme:
        normalized = f"{parsed.scheme.lower()}://{domain}{parsed.path or ''}"
    else:
        normalized = f"{domain}{parsed.path or ''}"
    if parsed.query:
        normalized = f"{normalized}?{parsed.query}"
    return normalized, domain


def extract_urls(text: str) -> tuple[UrlMatch, ...]:
    normalized_text = _normalize_obfuscated_text(text)
    matches: list[UrlMatch] = []
    seen: set[tuple[str, str]] = set()
    for match in URL_RE.finditer(normalized_text):
        raw = match.group(0).rstrip(".,;:!?")
        parsed = _split_url(raw)
        if parsed is None:
            continue
        normalized, domain = parsed
        dedupe_key = (normalized, domain)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        matches.append(UrlMatch(raw=raw, normalized=normalized, domain=domain))
    return tuple(matches)


def classify_domain(
    domain: str,
    *,
    allowed_domains: tuple[str, ...] = (),
    blocked_domains: tuple[str, ...] = (),
) -> DomainClassification:
    if any(_domain_matches(domain, blocked) for blocked in blocked_domains):
        return DomainClassification.BLOCKED
    if any(_domain_matches(domain, allowed) for allowed in allowed_domains):
        return DomainClassification.ALLOWED
    return DomainClassification.UNKNOWN


def defang_domain(domain: str) -> str:
    return domain.lower().strip(".").replace(".", "[.]")


def detect_discord_invites(text: str) -> tuple[DiscordInvite, ...]:
    invites: list[DiscordInvite] = []
    for url in extract_urls(text):
        if url.domain not in DISCORD_INVITE_DOMAINS:
            continue
        parsed = urlsplit(url.normalized if "://" in url.normalized else f"//{url.normalized}")
        path_parts = [part for part in parsed.path.split("/") if part]
        code: str | None = None
        if url.domain == "discord.gg" and path_parts:
            code = path_parts[0]
        elif len(path_parts) >= 2 and path_parts[0].lower() == "invite":
            code = path_parts[1]
        if code:
            invites.append(DiscordInvite(domain=url.domain, code=code))
    return tuple(invites)


def _extension_for(filename: str) -> str:
    lowered = filename.lower()
    if "." not in lowered:
        return ""
    return "." + lowered.rsplit(".", 1)[-1]


def extract_image_attachments(attachments: Any) -> tuple[AttachmentMetadata, ...]:
    images: list[AttachmentMetadata] = []
    for attachment in attachments:
        filename = str(getattr(attachment, "filename", "") or "")
        content_type = getattr(attachment, "content_type", None)
        extension = _extension_for(filename)
        is_image = (
            isinstance(content_type, str)
            and content_type.lower().startswith("image/")
        ) or extension in IMAGE_EXTENSIONS
        if not is_image:
            continue
        images.append(
            AttachmentMetadata(
                filename=filename,
                content_type=content_type,
                url=getattr(attachment, "url", None),
                size=getattr(attachment, "size", None),
                width=getattr(attachment, "width", None),
                height=getattr(attachment, "height", None),
                extension=extension,
                is_image=True,
            )
        )
    return tuple(images)


def _format_seconds(seconds: float) -> str:
    return str(int(seconds)) if float(seconds).is_integer() else f"{seconds:.1f}"


def decide_burst_threshold(
    *,
    event_times: tuple[float, ...],
    now: float,
    window_seconds: float,
    max_events: int,
    action: ModerationAction = ModerationAction.DELETE,
) -> ModerationDecision:
    recent = tuple(event_time for event_time in event_times if now - event_time <= window_seconds)
    if len(recent) < max_events:
        return ModerationDecision.allow("burst_threshold_not_met")
    return ModerationDecision(
        action=action,
        reason="burst_threshold",
        details=f"{len(recent)} events in {_format_seconds(window_seconds)}s",
    )


@dataclass(frozen=True)
class ImagePost:
    posted_at: float
    channel_id: int
    # (size, width, height) per image: Discord names every pasted screenshot "image.png",
    # so the filename is useless for spotting the same picture reposted.
    signatures: tuple[tuple[int | None, int | None, int | None], ...]


def image_signature(attachment: AttachmentMetadata) -> tuple[int | None, int | None, int | None]:
    return (attachment.size, attachment.width, attachment.height)


def confirm_image_burst(
    posts: "tuple[ImagePost, ...] | list[ImagePost]",
    *,
    now: float,
    window_seconds: float,
    min_images: int,
) -> str | None:
    """Second look after the settle delay. The raw threshold (3 images / 2 messages)
    also catches people sharing a few screenshots, so only act on spam-shaped bursts.
    Returns why the burst is confirmed, or None to let it go."""
    recent = [post for post in posts if now - post.posted_at <= window_seconds]
    channels = {post.channel_id for post in recent}
    if len(channels) >= 2:
        return f"images posted in {len(channels)} channels"
    seen: dict[tuple[int | None, int | None, int | None], int] = defaultdict(int)
    for post in recent:
        for signature in set(post.signatures):
            if signature[0] is not None:
                seen[signature] += 1
    if any(count >= 2 for count in seen.values()):
        return "same image reposted"
    total = sum(len(post.signatures) for post in recent)
    if total >= min_images * 2 and len(recent) >= 3:
        return f"{total} images across {len(recent)} messages"
    return None


_EVERYONE_OR_HERE_RE = re.compile(r"@(?:everyone|here)\b")
_CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
# Pasted logs/code in ``` fences or `inline` spans are exempt from the caps/emoji/newline checks.
_CODE_RE = re.compile(r"```.*?(?:```|$)|`[^`\n]*`", re.DOTALL)
_UNICODE_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "]"
)


def _normalize_for_duplicate(content: str) -> str:
    value = _strip_zero_width(content).casefold()
    return re.sub(r"\s+", " ", value).strip()


@lru_cache(maxsize=64)
def _compile_banned_words(words: tuple[str, ...]) -> tuple[re.Pattern, ...]:
    """A `*` splits the word into literal parts joined by \\w*, so it only bridges
    within one token (a comma, space, etc. still breaks the match)."""
    patterns = []
    for word in words:
        parts = [re.escape(part) for part in word.strip().split("*")]
        patterns.append(re.compile(r"(?i)\b" + r"\w*".join(parts) + r"\b"))
    return tuple(patterns)


def _matched_banned_word(content: str, words: tuple[str, ...]) -> str | None:
    if not words:
        return None
    text = _strip_zero_width(content)
    for word, pattern in zip(words, _compile_banned_words(words)):
        if pattern.search(text):
            return word
    return None


def _count_emoji(content: str) -> int:
    without_custom, custom_count = _CUSTOM_EMOJI_RE.subn("", content)
    return custom_count + len(_UNICODE_EMOJI_RE.findall(without_custom))


def _zalgo_combining_marks(content: str) -> int:
    return sum(1 for char in content if unicodedata.combining(char))


def _check_content_rules(signal: MessageSignal, config: ModerationConfig) -> ModerationDecision:
    """Static per-message filters; order here is the precedence among them."""
    content = signal.content

    banned = _matched_banned_word(content, tuple(config.banned_words))
    if banned is not None:
        return ModerationDecision(action=ModerationAction.DELETE, reason="banned_word", details=f"matched {banned!r}")

    if config.mass_mention_limit > 0 and signal.mention_count >= config.mass_mention_limit:
        return ModerationDecision(
            action=ModerationAction.DELETE,
            reason="mass_mention",
            details=f"{signal.mention_count} mentions",
        )

    if config.block_everyone_ping and not signal.can_mention_everyone and _EVERYONE_OR_HERE_RE.search(content):
        return ModerationDecision(action=ModerationAction.DELETE, reason="everyone_ping")

    prose = _CODE_RE.sub("", content)
    letters = [char for char in prose if char.isalpha()]
    if config.caps_percent > 0 and len(letters) >= config.caps_min_length:
        if letters:
            caps_percent = sum(char.isupper() for char in letters) / len(letters) * 100
            if caps_percent >= config.caps_percent:
                return ModerationDecision(
                    action=ModerationAction.DELETE,
                    reason="excessive_caps",
                    details=f"{caps_percent:.0f}% caps in {len(letters)} letters",
                )

    if config.emoji_limit > 0:
        emoji_count = _count_emoji(prose)
        if emoji_count >= config.emoji_limit:
            return ModerationDecision(
                action=ModerationAction.DELETE, reason="excessive_emoji", details=f"{emoji_count} emoji"
            )

    if config.newline_limit > 0:
        newline_count = prose.count("\n")
        if newline_count >= config.newline_limit:
            return ModerationDecision(
                action=ModerationAction.DELETE, reason="wall_of_text", details=f"{newline_count} newlines"
            )

    if config.zalgo_enabled:
        combining = _zalgo_combining_marks(content)
        base_chars = sum(1 for char in content if not char.isspace() and not unicodedata.combining(char))
        if base_chars and combining >= 8 and combining >= base_chars * 2:
            return ModerationDecision(
                action=ModerationAction.DELETE, reason="zalgo", details=f"{combining} combining marks"
            )

    return ModerationDecision.allow()


def _check_duplicate_spam(
    signal: MessageSignal, config: ModerationConfig, state: ModerationState, *, now: float
) -> ModerationDecision:
    if config.duplicate_count <= 0:
        return ModerationDecision.allow()
    normalized = _normalize_for_duplicate(signal.content)
    if len(normalized) < 3:
        return ModerationDecision.allow()
    key = (signal.guild_id, signal.author_id)
    events = [event for event in state.duplicate_events[key] if now - event[0] <= config.duplicate_window_seconds]
    events.append((now, normalized, signal.channel_id))
    state.duplicate_events[key] = events
    matches = [event for event in events if event[1] == normalized]
    if len(matches) < config.duplicate_count:
        return ModerationDecision.allow()
    channels = {event[2] for event in matches}
    # Copies spread across channels are the compromised-account/spam-bot shape; one channel is a
    # human repeating themselves.
    action = ModerationAction.TIMEOUT if len(channels) >= 2 else ModerationAction.DELETE
    return ModerationDecision(
        action=action,
        reason="duplicate_spam",
        details=f"{len(matches)} duplicate messages across {len(channels)} channel(s)",
    )


def _check_fast_messages(
    signal: MessageSignal, config: ModerationConfig, state: ModerationState, *, now: float
) -> ModerationDecision:
    if config.fast_message_count <= 0:
        return ModerationDecision.allow()
    key = (signal.guild_id, signal.author_id)
    events = state.record(state.fast_message_events, key, now, config.fast_message_window_seconds)
    decision = decide_burst_threshold(
        event_times=events,
        now=now,
        window_seconds=config.fast_message_window_seconds,
        max_events=config.fast_message_count,
        action=ModerationAction.DELETE,
    )
    if decision.action is ModerationAction.ALLOW:
        return decision
    return ModerationDecision(action=ModerationAction.DELETE, reason="fast_messages", details=decision.details)


def evaluate_message(
    signal: MessageSignal,
    config: ModerationConfig,
    state: ModerationState,
    *,
    now: float,
) -> ModerationDecision:
    urls = extract_urls(signal.content)
    domains = tuple(sorted({url.domain for url in urls}))
    defanged_domains = tuple(defang_domain(domain) for domain in domains)
    blocked_domains = tuple(
        domain
        for domain in domains
        if classify_domain(
            domain,
            allowed_domains=config.allowed_domains,
            blocked_domains=config.blocked_domains,
        )
        is DomainClassification.BLOCKED
    )
    if blocked_domains:
        return ModerationDecision(
            action=ModerationAction.DELETE,
            reason="blocked_domain",
            domains=blocked_domains,
            defanged_domains=tuple(defang_domain(domain) for domain in blocked_domains),
        )

    invites = detect_discord_invites(signal.content)
    if invites and config.block_discord_invites:
        return ModerationDecision(
            action=ModerationAction.DELETE,
            reason="discord_invite",
            domains=tuple(invite.domain for invite in invites),
            defanged_domains=tuple(defang_domain(invite.domain) for invite in invites),
            invites=invites,
        )

    content_decision = _check_content_rules(signal, config)
    if content_decision.action is not ModerationAction.ALLOW:
        return content_decision

    key = (signal.guild_id, signal.author_id)
    if urls:
        link_events = state.record(
            state.link_events,
            key,
            now,
            config.link_burst_window_seconds,
        )
        link_decision = decide_burst_threshold(
            event_times=link_events,
            now=now,
            window_seconds=config.link_burst_window_seconds,
            max_events=config.link_burst_count,
            action=ModerationAction.TIMEOUT,
        )
        if config.link_burst_count > 0 and link_decision.action is not ModerationAction.ALLOW:
            return ModerationDecision(
                action=link_decision.action,
                reason="link burst",
                details=link_decision.details,
                domains=domains,
                defanged_domains=defanged_domains,
            )

        suspicious = tuple(
            domain
            for domain in domains
            if any(_domain_matches(domain, shortener) for shortener in config.suspicious_shortener_domains)
        )
        if suspicious:
            return ModerationDecision(
                action=ModerationAction.ALERT,
                reason="suspicious_shortener",
                domains=suspicious,
                defanged_domains=tuple(defang_domain(domain) for domain in suspicious),
            )

    duplicate_decision = _check_duplicate_spam(signal, config, state, now=now)
    if duplicate_decision.action is not ModerationAction.ALLOW:
        return duplicate_decision

    fast_message_decision = _check_fast_messages(signal, config, state, now=now)
    if fast_message_decision.action is not ModerationAction.ALLOW:
        return fast_message_decision

    images = extract_image_attachments(signal.attachments)
    if images and config.image_burst_count > 0:
        # Every image counts toward the burst, even when the message also has
        # text, so spammers cannot dodge detection by attaching captions or
        # batching several images into one message.
        image_events = state.record(
            state.image_events,
            key,
            now,
            config.image_burst_window_seconds,
            count=len(images),
        )
        message_events = state.record(
            state.image_message_events,
            key,
            now,
            config.image_burst_window_seconds,
        )
        image_decision = decide_burst_threshold(
            event_times=image_events,
            now=now,
            window_seconds=config.image_burst_window_seconds,
            max_events=config.image_burst_count,
            action=ModerationAction.TIMEOUT,
        )
        if (
            image_decision.action is not ModerationAction.ALLOW
            and len(message_events) >= config.image_burst_min_messages
        ):
            return ModerationDecision(
                action=image_decision.action,
                reason="image burst",
                details=(
                    f"{len(image_events)} images across {len(message_events)} messages "
                    f"in {_format_seconds(config.image_burst_window_seconds)}s"
                ),
                image_count=len(images),
            )

    return ModerationDecision.allow()
