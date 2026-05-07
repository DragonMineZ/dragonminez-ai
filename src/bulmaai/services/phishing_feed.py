import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

from bulmaai.services import http


log = logging.getLogger(__name__)

DEFAULT_DOMAIN_FEED_URL = "https://phish.co.za/latest/phishing-domains-ACTIVE.txt"
DEFAULT_URL_FEED_URL = "https://phish.co.za/latest/phishing-links-ACTIVE.txt"
DEFAULT_DOMAIN_SHA256_URL = "https://raw.githubusercontent.com/Phishing-Database/checksums/master/phishing-domains-ACTIVE.txt.sha256"
DEFAULT_URL_SHA256_URL = "https://raw.githubusercontent.com/Phishing-Database/checksums/master/phishing-links-ACTIVE.txt.sha256"
DOMAINS_CACHE_FILE = "phishing-domains-ACTIVE.txt"
URLS_CACHE_FILE = "phishing-links-ACTIVE.txt"
METADATA_CACHE_FILE = "metadata.json"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_domain(value: str) -> str | None:
    raw = value.strip().lower().strip(".")
    if not raw or raw.startswith("#") or " " in raw or "/" in raw:
        return None
    if "@" in raw:
        raw = raw.rsplit("@", 1)[-1]
    if ":" in raw:
        raw = raw.split(":", 1)[0]
    if not raw or "." not in raw:
        return None
    try:
        return raw.encode("idna").decode("ascii")
    except UnicodeError:
        return None


def canonicalize_url(value: str) -> str | None:
    raw = value.strip()
    if not raw or raw.startswith("#"):
        return None
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        return None
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        return None
    host = normalize_domain(parsed.hostname or "")
    if host is None:
        return None
    netloc = host
    try:
        port = parsed.port
    except ValueError:
        return None
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{netloc}:{port}"
    return urlunsplit((scheme, netloc, parsed.path or "", parsed.query, ""))


def _feed_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


def parse_domain_feed(text: str) -> frozenset[str]:
    domains = {domain for line in _feed_lines(text) if (domain := normalize_domain(line))}
    return frozenset(domains)


def parse_url_feed(text: str) -> frozenset[str]:
    urls = {url for line in _feed_lines(text) if (url := canonicalize_url(line))}
    return frozenset(urls)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _expected_checksum(text: str) -> str | None:
    first = text.strip().split(maxsplit=1)[0] if text.strip() else ""
    lowered = first.lower()
    if len(lowered) == 64 and all(char in "0123456789abcdef" for char in lowered):
        return lowered
    return None


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)


@dataclass(frozen=True)
class PhishingFeedSnapshot:
    domains: frozenset[str] = frozenset()
    exact_urls: frozenset[str] = frozenset()
    loaded_at: datetime | None = None
    last_success_at: datetime | None = None
    stale: bool = False
    counts: Mapping[str, int] = field(default_factory=dict)
    checksums: Mapping[str, str] = field(default_factory=dict)
    error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "domains", frozenset(self.domains))
        object.__setattr__(self, "exact_urls", frozenset(self.exact_urls))
        object.__setattr__(self, "counts", MappingProxyType(dict(self.counts)))
        object.__setattr__(self, "checksums", MappingProxyType(dict(self.checksums)))

    @classmethod
    def empty(cls, *, error: str | None = None) -> "PhishingFeedSnapshot":
        return cls(
            loaded_at=_now_utc(),
            stale=True,
            counts={"domains": 0, "exact_urls": 0},
            error=error,
        )

    @property
    def verified(self) -> bool:
        return bool(self.domains or self.exact_urls) and self.error is None


class PhishingFeedService:
    def __init__(
        self,
        *,
        cache_dir: str | Path,
        max_stale_hours: int,
        domain_feed_url: str = DEFAULT_DOMAIN_FEED_URL,
        url_feed_url: str = DEFAULT_URL_FEED_URL,
        domain_checksum_url: str | None = DEFAULT_DOMAIN_SHA256_URL,
        url_checksum_url: str | None = DEFAULT_URL_SHA256_URL,
        timeout_seconds: int = 20,
    ):
        self.cache_dir = Path(cache_dir)
        self.max_stale_hours = max(1, int(max_stale_hours))
        self.domain_feed_url = domain_feed_url
        self.url_feed_url = url_feed_url
        self.domain_checksum_url = domain_checksum_url
        self.url_checksum_url = url_checksum_url
        self.timeout_seconds = timeout_seconds
        self._snapshot = PhishingFeedSnapshot.empty()

    @property
    def snapshot(self) -> PhishingFeedSnapshot:
        return self._snapshot

    def load_cache(self) -> PhishingFeedSnapshot:
        try:
            domains_text = (self.cache_dir / DOMAINS_CACHE_FILE).read_text(encoding="utf-8")
            urls_text = (self.cache_dir / URLS_CACHE_FILE).read_text(encoding="utf-8")
            metadata = self._load_metadata()
            snapshot = self._snapshot_from_text(
                domains_text,
                urls_text,
                loaded_at=_now_utc(),
                last_success_at=_parse_datetime(metadata.get("last_success_at")),
                checksums=dict(metadata.get("checksums") or {}),
            )
        except (OSError, json.JSONDecodeError, TypeError) as error:
            snapshot = PhishingFeedSnapshot.empty(error=str(error))
        self._snapshot = snapshot
        return snapshot

    async def refresh(self) -> PhishingFeedSnapshot:
        previous = self._snapshot
        try:
            domains_text, urls_text, checksums = await self._download_feeds()
            snapshot = self._snapshot_from_text(
                domains_text,
                urls_text,
                loaded_at=_now_utc(),
                last_success_at=_now_utc(),
                checksums=checksums,
            )
            _atomic_write(self.cache_dir / DOMAINS_CACHE_FILE, domains_text)
            _atomic_write(self.cache_dir / URLS_CACHE_FILE, urls_text)
            _atomic_write(
                self.cache_dir / METADATA_CACHE_FILE,
                json.dumps(
                    {
                        "last_success_at": snapshot.last_success_at.isoformat()
                        if snapshot.last_success_at
                        else None,
                        "checksums": dict(snapshot.checksums),
                    },
                    indent=2,
                    sort_keys=True,
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.warning("Phishing feed refresh failed: %s", error)
            if previous.domains or previous.exact_urls:
                snapshot = PhishingFeedSnapshot(
                    domains=previous.domains,
                    exact_urls=previous.exact_urls,
                    loaded_at=_now_utc(),
                    last_success_at=previous.last_success_at,
                    stale=self._is_stale(previous.last_success_at),
                    counts=previous.counts,
                    checksums=previous.checksums,
                    error=str(error),
                )
            else:
                snapshot = PhishingFeedSnapshot.empty(error=str(error))
        self._snapshot = snapshot
        return snapshot

    def _load_metadata(self) -> dict[str, object]:
        path = self.cache_dir / METADATA_CACHE_FILE
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}

    def _is_stale(self, last_success_at: datetime | None) -> bool:
        if last_success_at is None:
            return True
        age = _now_utc() - last_success_at
        return age.total_seconds() > self.max_stale_hours * 3600

    def _snapshot_from_text(
        self,
        domains_text: str,
        urls_text: str,
        *,
        loaded_at: datetime,
        last_success_at: datetime | None,
        checksums: Mapping[str, str],
    ) -> PhishingFeedSnapshot:
        domains = parse_domain_feed(domains_text)
        exact_urls = parse_url_feed(urls_text)
        return PhishingFeedSnapshot(
            domains=domains,
            exact_urls=exact_urls,
            loaded_at=loaded_at,
            last_success_at=last_success_at,
            stale=self._is_stale(last_success_at),
            counts={"domains": len(domains), "exact_urls": len(exact_urls)},
            checksums=checksums,
        )

    async def _download_feeds(self) -> tuple[str, str, dict[str, str]]:
        domain_task = http.request("GET", self.domain_feed_url, timeout=self.timeout_seconds)
        url_task = http.request("GET", self.url_feed_url, timeout=self.timeout_seconds)
        domain_response, url_response = await asyncio.gather(domain_task, url_task)
        domain_response.raise_for_status()
        url_response.raise_for_status()
        domains_text = domain_response.text
        urls_text = url_response.text
        checksums = {
            "domains_sha256": _sha256(domains_text),
            "exact_urls_sha256": _sha256(urls_text),
        }
        await self._verify_checksum(self.domain_checksum_url, domains_text, "domains")
        await self._verify_checksum(self.url_checksum_url, urls_text, "exact_urls")
        return domains_text, urls_text, checksums

    async def _verify_checksum(self, checksum_url: str | None, text: str, name: str) -> None:
        if not checksum_url:
            return
        response = await http.request("GET", checksum_url, timeout=self.timeout_seconds)
        response.raise_for_status()
        expected = _expected_checksum(response.text)
        if expected is None:
            return
        actual = _sha256(text)
        if actual != expected:
            raise ValueError(f"{name} checksum mismatch")
