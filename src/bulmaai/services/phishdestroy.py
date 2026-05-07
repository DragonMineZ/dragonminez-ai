import asyncio
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from bulmaai.services import http


DEFAULT_BASE_URL = "https://api.destroy.tools"
DEFAULT_RISK_SCORE_THRESHOLD = 70


class PhishDestroyUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class PhishDestroyVerdict:
    domain: str
    threat: bool
    risk_score: int = 0
    severity: str = ""
    active: bool | None = None
    flags: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _CachedVerdict:
    verdict: PhishDestroyVerdict
    expires_at: float


def normalize_domain(value: str) -> str | None:
    raw = value.strip()
    if not raw:
        return None
    parsed = urlsplit(raw if "://" in raw else f"//{raw}")
    domain = (parsed.hostname or "").lower().strip(".")
    if not domain or " " in domain or "." not in domain:
        return None
    try:
        return domain.encode("idna").decode("ascii")
    except UnicodeError:
        return None


class PhishDestroyClient:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: int = 5,
        safe_ttl_seconds: int = 6 * 3600,
        threat_ttl_seconds: int = 24 * 3600,
        risk_score_threshold: int = DEFAULT_RISK_SCORE_THRESHOLD,
        max_concurrency: int = 2,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.safe_ttl_seconds = max(1, int(safe_ttl_seconds))
        self.threat_ttl_seconds = max(1, int(threat_ttl_seconds))
        self.risk_score_threshold = max(1, int(risk_score_threshold))
        self._cache: dict[str, _CachedVerdict] = {}
        self._semaphore = asyncio.Semaphore(max(1, int(max_concurrency)))

    async def check_domain(self, value: str) -> PhishDestroyVerdict:
        domain = normalize_domain(value)
        if domain is None:
            return PhishDestroyVerdict(domain=value, threat=False)

        now = time.monotonic()
        cached = self._cache.get(domain)
        if cached and cached.expires_at > now:
            return cached.verdict

        verdict = await self._fetch_verdict(domain)
        ttl = self.threat_ttl_seconds if verdict.threat else self.safe_ttl_seconds
        self._cache[domain] = _CachedVerdict(verdict=verdict, expires_at=now + ttl)
        return verdict

    async def healthcheck(self) -> None:
        await self._fetch_verdict("example.com")

    async def _fetch_verdict(self, domain: str) -> PhishDestroyVerdict:
        async with self._semaphore:
            try:
                response = await http.request(
                    "GET",
                    f"{self.base_url}/v1/check",
                    params={"domain": domain},
                    headers={"User-Agent": "BulmaAI Discord moderation"},
                    timeout=self.timeout_seconds,
                )
            except Exception as error:
                raise PhishDestroyUnavailable(str(error)) from error

        if response.status_code == 429 or response.status_code >= 500:
            raise PhishDestroyUnavailable(f"HTTP {response.status_code}")
        if response.status_code >= 400:
            return PhishDestroyVerdict(domain=domain, threat=False)

        try:
            payload = response.json()
        except ValueError as error:
            raise PhishDestroyUnavailable("invalid JSON response") from error
        if not isinstance(payload, dict):
            raise PhishDestroyUnavailable("unexpected response payload")

        risk_score = _int_value(payload.get("risk_score"))
        threat = bool(payload.get("threat")) or risk_score >= self.risk_score_threshold
        flags = payload.get("flags")
        return PhishDestroyVerdict(
            domain=str(payload.get("domain") or domain),
            threat=threat,
            risk_score=risk_score,
            severity=str(payload.get("severity") or ""),
            active=payload.get("active") if isinstance(payload.get("active"), bool) else None,
            flags=tuple(str(flag) for flag in flags) if isinstance(flags, list) else (),
            raw=dict(payload),
        )


def _int_value(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0
