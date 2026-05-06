import re
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from urllib.parse import urlsplit


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


@dataclass(frozen=True)
class ModerationConfig:
    blocked_domains: tuple[str, ...] = ()
    allowed_domains: tuple[str, ...] = ()
    block_discord_invites: bool = False
    suspicious_shortener_domains: tuple[str, ...] = DEFAULT_SUSPICIOUS_SHORTENERS
    image_burst_count: int = 3
    image_burst_window_seconds: int = 20
    link_burst_count: int = 5
    link_burst_window_seconds: int = 60


@dataclass(frozen=True)
class ModerationDecision:
    action: ModerationAction
    reason: str
    details: str = ""
    domains: tuple[str, ...] = ()
    defanged_domains: tuple[str, ...] = ()
    invites: tuple[DiscordInvite, ...] = ()
    image_count: int = 0

    @classmethod
    def allow(cls, reason: str = "allowed") -> "ModerationDecision":
        return cls(action=ModerationAction.ALLOW, reason=reason)


@dataclass
class ModerationState:
    image_events: dict[tuple[int, int], list[float]] = field(default_factory=lambda: defaultdict(list))
    link_events: dict[tuple[int, int], list[float]] = field(default_factory=lambda: defaultdict(list))

    def record(self, bucket: dict[tuple[int, int], list[float]], key: tuple[int, int], now: float, window_seconds: float) -> tuple[float, ...]:
        events = [event_time for event_time in bucket[key] if now - event_time <= window_seconds]
        events.append(now)
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


def _message_has_only_images(signal: MessageSignal, image_count: int) -> bool:
    return image_count > 0 and not signal.content.strip()


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
        )
        if link_decision.action is not ModerationAction.ALLOW:
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

    images = extract_image_attachments(signal.attachments)
    if _message_has_only_images(signal, len(images)):
        image_events = state.record(
            state.image_events,
            key,
            now,
            config.image_burst_window_seconds,
        )
        image_decision = decide_burst_threshold(
            event_times=image_events,
            now=now,
            window_seconds=config.image_burst_window_seconds,
            max_events=config.image_burst_count,
        )
        if image_decision.action is not ModerationAction.ALLOW:
            return ModerationDecision(
                action=image_decision.action,
                reason="image burst",
                details=image_decision.details,
                image_count=len(images),
            )

    return ModerationDecision.allow()
