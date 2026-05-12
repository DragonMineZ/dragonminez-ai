import hashlib
import hmac
import json
import re
import secrets
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urlencode

from bulmaai.services.http import request


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
    workflow_run_url: str | None = None


@dataclass(frozen=True, slots=True)
class DevJarOAuthState:
    artifact: DevJarArtifact
    requester_id: int
    expires_at: int


class PatreonOAuthClient:
    token_url = "https://www.patreon.com/api/oauth2/token"
    identity_url = "https://www.patreon.com/api/oauth2/v2/identity"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri

    async def fetch_identity_for_code(self, code: str) -> dict[str, Any]:
        token_response = await request(
            "POST",
            self.token_url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": self.redirect_uri,
            },
        )
        token_response.raise_for_status()
        access_token = str(token_response.json()["access_token"])

        identity_response = await request(
            "GET",
            self.identity_url,
            headers={"Authorization": f"Bearer {access_token}"},
            params={
                "include": "memberships,memberships.campaign",
                "fields[member]": (
                    "patron_status,last_charge_status,currently_entitled_amount_cents"
                ),
                "fields[campaign]": "creation_name,vanity,url",
            },
        )
        identity_response.raise_for_status()
        payload = identity_response.json()
        if not isinstance(payload, dict):
            raise ValueError("Patreon identity response must be an object")
        return payload


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

    if not remote_name:
        raise ValueError("remote_name is required")
    if not re.fullmatch(r"[a-f0-9]{64}", sha256):
        raise ValueError("sha256 must be a 64-character lowercase hex digest")
    if workflow_run_url is not None and not workflow_run_url.startswith(("http://", "https://")):
        raise ValueError("workflow_run_url must be an HTTP(S) URL")

    return DevJarUploadPayload(
        artifact=parse_dev_jar_filename(remote_name),
        sha256=sha256,
        workflow_run_url=workflow_run_url,
    )


def _b64encode_json(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode_json(value: str) -> dict[str, Any]:
    padding = "=" * (-len(value) % 4)
    decoded = urlsafe_b64decode((value + padding).encode("ascii"))
    payload = json.loads(decoded.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("OAuth state payload must be an object")
    return payload


def _sign_state(secret: str, body: str) -> str:
    return hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()


def build_oauth_state(
    *,
    secret: str,
    artifact: DevJarArtifact,
    requester_id: int,
    expires_at: int,
) -> str:
    body = _b64encode_json(
        {
            "file_name": artifact.file_name,
            "requester_id": int(requester_id),
            "expires_at": int(expires_at),
        }
    )
    return f"{body}.{_sign_state(secret, body)}"


def parse_oauth_state(
    secret: str,
    state: str,
    *,
    now: Callable[[], float] = monotonic,
) -> DevJarOAuthState | None:
    try:
        body, signature = state.rsplit(".", 1)
    except ValueError:
        return None
    expected = _sign_state(secret, body)
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = _b64decode_json(body)
        expires_at = int(payload["expires_at"])
        if expires_at < now():
            return None
        return DevJarOAuthState(
            artifact=parse_dev_jar_filename(str(payload["file_name"])),
            requester_id=int(payload["requester_id"]),
            expires_at=expires_at,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def build_patreon_authorization_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    scope: str = "identity identity.memberships",
) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
        }
    )
    return f"https://www.patreon.com/oauth2/authorize?{query}"


def _member_matches_campaign(member: dict[str, Any], campaign_id: str | None) -> bool:
    if campaign_id is None:
        return True
    campaign = ((member.get("relationships") or {}).get("campaign") or {}).get("data") or {}
    return str(campaign.get("id") or "") == str(campaign_id)


def has_active_patreon_membership(
    identity_payload: dict[str, Any],
    *,
    campaign_id: str | None = None,
) -> bool:
    included = identity_payload.get("included")
    if not isinstance(included, list):
        return False

    for item in included:
        if not isinstance(item, dict) or item.get("type") != "member":
            continue
        if not _member_matches_campaign(item, campaign_id):
            continue
        attrs = item.get("attributes") or {}
        patron_status = str(attrs.get("patron_status") or "").lower()
        charge_status = str(attrs.get("last_charge_status") or "").lower()
        amount_cents = int(attrs.get("currently_entitled_amount_cents") or 0)
        if patron_status == "active_patron" and (
            amount_cents > 0 or charge_status in {"paid", "pending"}
        ):
            return True
    return False
