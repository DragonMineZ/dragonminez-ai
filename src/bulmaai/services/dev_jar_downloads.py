import hashlib
import re
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any


DEV_JAR_FILENAME_RE = re.compile(
    r"^dragonminez-(?P<version>.+)__(?P<branch>[a-z0-9._-]+)__(?P<sha>[a-f0-9]{12})\.jar$"
)


@dataclass(frozen=True, slots=True)
class DevJarArtifact:
    file_name: str
    version: str
    branch_slug: str
    commit_sha: str
    size_bytes: int | None = None
    modified_at: datetime | None = None

    def resolve_path(self, upload_dir: Path | str) -> Path:
        base = Path(upload_dir).resolve()
        path = (base / self.file_name).resolve()
        if path.parent != base:
            raise ValueError("Artifact path escapes upload directory")
        return path


@dataclass(frozen=True, slots=True)
class DevJarCommit:
    sha: str
    title: str
    description: str | None
    author: str
    url: str


@dataclass(frozen=True, slots=True)
class DevJarDownloadGrant:
    artifact: DevJarArtifact
    requester_id: int
    expires_at: float


@dataclass(frozen=True, slots=True)
class DevJarDownloadClaim:
    token_hash: str
    artifact: DevJarArtifact


@dataclass(frozen=True, slots=True)
class DevJarUploadPayload:
    artifact: DevJarArtifact
    sha256: str
    commits: tuple[DevJarCommit, ...]
    workflow_run_url: str | None = None


class OneTimeDownloadTokenStore:
    def __init__(self, *, now: Callable[[], float] = monotonic):
        self._now = now
        self._grants: dict[str, DevJarDownloadGrant] = {}
        self._claimed: set[str] = set()

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def issue(
        self,
        *,
        artifact: DevJarArtifact,
        requester_id: int,
        ttl_seconds: int,
    ) -> str:
        token = secrets.token_urlsafe(32)
        self._grants[self._hash_token(token)] = DevJarDownloadGrant(
            artifact=artifact,
            requester_id=requester_id,
            expires_at=self._now() + ttl_seconds,
        )
        return token

    def consume(self, token: str) -> DevJarArtifact | None:
        token_hash = self._hash_token(token)
        grant = self._grants.pop(token_hash, None)
        self._claimed.discard(token_hash)
        if grant is None:
            return None
        if grant.expires_at < self._now():
            return None
        return grant.artifact

    def peek(self, token: str) -> DevJarArtifact | None:
        token_hash = self._hash_token(token)
        if token_hash in self._claimed:
            return None
        grant = self._grants.get(token_hash)
        if grant is None:
            return None
        if grant.expires_at < self._now():
            self._grants.pop(token_hash, None)
            return None
        return grant.artifact

    def claim(self, token: str) -> DevJarDownloadClaim | None:
        token_hash = self._hash_token(token)
        if token_hash in self._claimed:
            return None
        grant = self._grants.get(token_hash)
        if grant is None:
            return None
        if grant.expires_at < self._now():
            self._grants.pop(token_hash, None)
            return None
        self._claimed.add(token_hash)
        return DevJarDownloadClaim(token_hash=token_hash, artifact=grant.artifact)

    def complete_claim(self, claim: DevJarDownloadClaim) -> None:
        self._grants.pop(claim.token_hash, None)
        self._claimed.discard(claim.token_hash)

    def release_claim(self, claim: DevJarDownloadClaim) -> None:
        self._claimed.discard(claim.token_hash)

    def cleanup_expired(self) -> None:
        now = self._now()
        for token_hash, grant in list(self._grants.items()):
            if grant.expires_at < now:
                self._grants.pop(token_hash, None)
                self._claimed.discard(token_hash)


def parse_dev_jar_filename(file_name: str) -> DevJarArtifact:
    if Path(file_name).name != file_name:
        raise ValueError("Artifact file name must not contain path separators")

    match = DEV_JAR_FILENAME_RE.fullmatch(file_name)
    if match is None:
        raise ValueError("Artifact file name does not match DragonMineZ dev jar format")

    return DevJarArtifact(
        file_name=file_name,
        version=match.group("version"),
        branch_slug=match.group("branch"),
        commit_sha=match.group("sha"),
    )


def _artifact_from_path(path: Path) -> DevJarArtifact | None:
    try:
        artifact = parse_dev_jar_filename(path.name)
    except ValueError:
        return None
    if not path.is_file():
        return None
    stat = path.stat()
    return DevJarArtifact(
        file_name=artifact.file_name,
        version=artifact.version,
        branch_slug=artifact.branch_slug,
        commit_sha=artifact.commit_sha,
        size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
    )


def iter_dev_jars(upload_dir: Path | str) -> Iterable[DevJarArtifact]:
    base = Path(upload_dir)
    if not base.exists():
        return ()
    return tuple(
        artifact
        for artifact in (_artifact_from_path(path) for path in base.iterdir())
        if artifact is not None
    )


def find_latest_dev_jar(upload_dir: Path | str) -> DevJarArtifact:
    artifacts = list(iter_dev_jars(upload_dir))
    if not artifacts:
        raise FileNotFoundError("No DragonMineZ dev jars found")
    return max(
        artifacts,
        key=lambda artifact: (
            artifact.modified_at or datetime.min.replace(tzinfo=timezone.utc),
            artifact.file_name,
        ),
    )


def parse_dev_jar_upload_payload(payload: dict[str, Any]) -> DevJarUploadPayload:
    remote_name = str(payload.get("remote_name") or "").strip()
    sha256 = str(payload.get("sha256") or "").strip().lower()
    workflow_run_url = str(payload.get("workflow_run_url") or "").strip() or None
    commits = _parse_dev_jar_commits(payload.get("commits"))

    if not remote_name:
        raise ValueError("remote_name is required")
    if not re.fullmatch(r"[a-f0-9]{64}", sha256):
        raise ValueError("sha256 must be a 64-character lowercase hex digest")
    if workflow_run_url is not None and not workflow_run_url.startswith(("http://", "https://")):
        raise ValueError("workflow_run_url must be an HTTP(S) URL")

    return DevJarUploadPayload(
        artifact=parse_dev_jar_filename(remote_name),
        sha256=sha256,
        commits=commits,
        workflow_run_url=workflow_run_url,
    )


def _required_commit_string(commit: dict[str, Any], field_name: str) -> str:
    value = str(commit.get(field_name) or "").strip()
    if not value:
        raise ValueError(f"commit {field_name} is required")
    return value


def _parse_dev_jar_commits(value: Any) -> tuple[DevJarCommit, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("commits must be a non-empty list")

    commits: list[DevJarCommit] = []
    for raw_commit in value:
        if not isinstance(raw_commit, dict):
            raise ValueError("each commit must be an object")
        sha = _required_commit_string(raw_commit, "sha").lower()
        title = _required_commit_string(raw_commit, "title")
        author = _required_commit_string(raw_commit, "author")
        url = _required_commit_string(raw_commit, "url")
        description = str(raw_commit.get("description") or "").strip() or None
        if not re.fullmatch(r"[a-f0-9]{7,40}", sha):
            raise ValueError("commit sha must be a 7 to 40 character lowercase hex string")
        if not url.startswith(("http://", "https://")):
            raise ValueError("commit url must be an HTTP(S) URL")
        commits.append(
            DevJarCommit(
                sha=sha,
                title=title,
                description=description,
                author=author,
                url=url,
            )
        )
    return tuple(commits)
