import html
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable

from bulmaai.config import Settings
from bulmaai.services.http import request

log = logging.getLogger(__name__)

CFWIDGET_API_BASE = "https://api.cfwidget.com"
CURSEFORGE_API_BASE = "https://api.curseforge.com/v1"
RELEASE_TYPE_NAMES = {
    1: "release",
    2: "beta",
    3: "alpha",
}
RELEASE_PRIORITY = {
    "release": 0,
    "beta": 1,
    "alpha": 2,
}
KNOWN_LOADERS = {"forge", "neoforge", "fabric", "quilt", "rift", "liteloader"}


@dataclass(slots=True, frozen=True)
class CurseForgeRelease:
    project_id: int
    project_slug: str
    project_title: str
    project_summary: str
    project_url: str
    project_thumbnail_url: str | None
    file_id: int
    file_display_name: str
    file_name: str
    file_page_url: str
    file_download_url: str | None
    release_type: str
    version_tags: tuple[str, ...]
    uploaded_at: datetime
    file_size_bytes: int | None
    download_count: int | None
    changelog_text: str | None
    source_name: str

    @property
    def minecraft_versions(self) -> tuple[str, ...]:
        return tuple(
            tag for tag in self.version_tags
            if re.fullmatch(r"\d+(?:\.\d+)+", tag)
        )

    @property
    def loader_tags(self) -> tuple[str, ...]:
        return tuple(tag for tag in self.version_tags if tag.lower() in KNOWN_LOADERS)

    @property
    def environment_tags(self) -> tuple[str, ...]:
        minecraft_versions = set(self.minecraft_versions)
        loader_tags = {tag.lower() for tag in self.loader_tags}
        return tuple(
            tag for tag in self.version_tags
            if tag not in minecraft_versions and tag.lower() not in loader_tags
        )


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_release_type(value: Any) -> str:
    if isinstance(value, str):
        lowered = value.strip().lower()
        return lowered if lowered else "unknown"
    normalized = RELEASE_TYPE_NAMES.get(_coerce_int(value))
    return normalized or "unknown"


def _strip_html(text: str) -> str:
    if not text:
        return ""

    normalized = text
    normalized = re.sub(r"<br\s*/?>", "\n", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"</p>", "\n", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"</div>", "\n", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"</li>", "\n", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"<li[^>]*>", "- ", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"<[^>]+>", "", normalized)
    normalized = html.unescape(normalized)
    normalized = normalized.replace("\r\n", "\n")
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def _coerce_changelog(payload: Any) -> str | None:
    raw_value = payload
    if isinstance(payload, dict):
        raw_value = payload.get("data")
    if not isinstance(raw_value, str):
        return None
    stripped = _strip_html(raw_value)
    return stripped or None


def _pick_latest_file(files: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    candidates = [
        entry for entry in files
        if isinstance(entry, dict) and entry.get("isAvailable", True) is not False
    ]
    if not candidates:
        return None

    def sort_key(entry: dict[str, Any]) -> tuple[int, float, int]:
        release_type = _normalize_release_type(entry.get("releaseType") or entry.get("type"))
        release_priority = RELEASE_PRIORITY.get(release_type, 99)
        uploaded_at = _parse_datetime(
            entry.get("fileDate")
            or entry.get("uploaded_at")
            or entry.get("uploadedAt")
        )
        uploaded_ts = -(uploaded_at.timestamp() if uploaded_at else 0.0)
        file_id = -(_coerce_int(entry.get("id")) or 0)
        return (release_priority, uploaded_ts, file_id)

    return min(candidates, key=sort_key)


class CurseForgeClient:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._official_api_warning_emitted = False

    async def fetch_latest_release(self) -> CurseForgeRelease:
        if self._settings.curseforge_api_key:
            try:
                return await self._fetch_latest_release_official()
            except Exception as exc:
                if not self._official_api_warning_emitted:
                    log.warning(
                        "Official CurseForge API unavailable, using CFWidget fallback instead: %s",
                        exc,
                    )
                    self._official_api_warning_emitted = True
        return await self._fetch_latest_release_cfwidget()

    async def _request_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        response = await request("GET", url, headers=headers, params=params)
        response.raise_for_status()
        return response.json()

    def _official_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._settings.curseforge_api_key:
            headers["x-api-key"] = self._settings.curseforge_api_key
        return headers

    async def _fetch_latest_release_official(self) -> CurseForgeRelease:
        payload = await self._request_json(
            f"{CURSEFORGE_API_BASE}/mods/{self._settings.curseforge_project_id}",
            headers=self._official_headers(),
        )
        mod = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(mod, dict):
            raise ValueError("Unexpected CurseForge mod payload")

        latest_file = _pick_latest_file(mod.get("latestFiles") or [])
        if latest_file is None:
            raise ValueError("No latest files returned by CurseForge")

        file_id = _coerce_int(latest_file.get("id"))
        if file_id is None:
            raise ValueError("CurseForge returned a latest file without an id")

        changelog_payload = await self._request_json(
            f"{CURSEFORGE_API_BASE}/mods/{self._settings.curseforge_project_id}/files/{file_id}/changelog",
            headers=self._official_headers(),
        )
        download_url_payload = await self._request_json(
            f"{CURSEFORGE_API_BASE}/mods/{self._settings.curseforge_project_id}/files/{file_id}/download-url",
            headers=self._official_headers(),
        )

        project_url = (
            (mod.get("links") or {}).get("websiteUrl")
            or f"https://www.curseforge.com/{self._settings.curseforge_project_slug}"
        )
        file_page_url = f"{project_url.rstrip('/')}/files/{file_id}"
        uploaded_at = _parse_datetime(latest_file.get("fileDate")) or datetime.now(UTC)

        download_url = latest_file.get("downloadUrl")
        if not isinstance(download_url, str) or not download_url.strip():
            if isinstance(download_url_payload, dict):
                download_url = download_url_payload.get("data")
            elif isinstance(download_url_payload, str):
                download_url = download_url_payload
            else:
                download_url = None

        logo = mod.get("logo") or {}
        version_tags = tuple(str(tag) for tag in latest_file.get("gameVersions") or [])

        return CurseForgeRelease(
            project_id=self._settings.curseforge_project_id,
            project_slug=self._settings.curseforge_project_slug,
            project_title=mod.get("name") or "DragonMineZ",
            project_summary=mod.get("summary") or "",
            project_url=project_url,
            project_thumbnail_url=logo.get("thumbnailUrl") or logo.get("url"),
            file_id=file_id,
            file_display_name=latest_file.get("displayName") or latest_file.get("fileName") or f"File {file_id}",
            file_name=latest_file.get("fileName") or latest_file.get("displayName") or f"file-{file_id}",
            file_page_url=file_page_url,
            file_download_url=download_url,
            release_type=_normalize_release_type(latest_file.get("releaseType")),
            version_tags=version_tags,
            uploaded_at=uploaded_at,
            file_size_bytes=_coerce_int(latest_file.get("fileLength")),
            download_count=_coerce_int(latest_file.get("downloadCount")),
            changelog_text=_coerce_changelog(changelog_payload),
            source_name="CurseForge API",
        )

    async def _fetch_latest_release_cfwidget(self) -> CurseForgeRelease:
        payload = await self._request_json(
            f"{CFWIDGET_API_BASE}/{self._settings.curseforge_project_id}",
        )
        if not isinstance(payload, dict):
            raise ValueError("Unexpected CFWidget payload")

        latest_file = payload.get("download")
        if not isinstance(latest_file, dict):
            latest_file = _pick_latest_file(payload.get("files") or [])
        if latest_file is None:
            raise ValueError("No files returned by CFWidget")

        file_id = _coerce_int(latest_file.get("id"))
        if file_id is None:
            raise ValueError("CFWidget returned a latest file without an id")

        urls = payload.get("urls") or {}
        project_url = (
            urls.get("project")
            or urls.get("curseforge")
            or f"https://www.curseforge.com/{self._settings.curseforge_project_slug}"
        )
        file_page_url = latest_file.get("url") or f"{project_url.rstrip('/')}/files/{file_id}"
        uploaded_at = _parse_datetime(latest_file.get("uploaded_at")) or datetime.now(UTC)
        version_tags = tuple(str(tag) for tag in latest_file.get("versions") or [])

        return CurseForgeRelease(
            project_id=self._settings.curseforge_project_id,
            project_slug=self._settings.curseforge_project_slug,
            project_title=payload.get("title") or "DragonMineZ",
            project_summary=payload.get("summary") or "",
            project_url=project_url,
            project_thumbnail_url=payload.get("thumbnail"),
            file_id=file_id,
            file_display_name=latest_file.get("display") or latest_file.get("name") or f"File {file_id}",
            file_name=latest_file.get("name") or latest_file.get("display") or f"file-{file_id}",
            file_page_url=file_page_url,
            file_download_url=None,
            release_type=_normalize_release_type(latest_file.get("type")),
            version_tags=version_tags,
            uploaded_at=uploaded_at,
            file_size_bytes=_coerce_int(latest_file.get("filesize")),
            download_count=_coerce_int(latest_file.get("downloads")),
            changelog_text=None,
            source_name="CFWidget fallback",
        )
