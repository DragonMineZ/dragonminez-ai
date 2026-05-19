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


DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_OAUTH_CLIENT_ID = "1336867824815312906"
DISCORD_OAUTH_REDIRECT_URI = "https://dragonminez.com/discord_oauth_callback"
DISCORD_AUTHORIZATION_URL = (
    "https://discord.com/oauth2/authorize?"
    "client_id=1336867824815312906&response_type=code&"
    "redirect_uri=https%3A%2F%2Fdragonminez.com%2Fdiscord_oauth_callback&"
    "scope=identify+guilds.members.read+guilds"
)
ADMINISTRATOR_PERMISSION = 0x8
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
    guild_id: int
    expires_at: int


@dataclass(frozen=True, slots=True)
class DiscordOAuthMember:
    user_id: int
    guild_id: int
    permissions: int
    role_ids: tuple[int, ...]


class DiscordOAuthClient:
    token_url = "https://discord.com/api/oauth2/token"

    def __init__(
        self,
        *,
        client_secret: str,
        client_id: str = DISCORD_OAUTH_CLIENT_ID,
        redirect_uri: str = DISCORD_OAUTH_REDIRECT_URI,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri

    async def fetch_member_for_code(self, code: str, *, guild_id: int) -> DiscordOAuthMember:
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
        headers = {"Authorization": f"Bearer {access_token}"}

        user_response = await request(
            "GET",
            f"{DISCORD_API_BASE}/users/@me",
            headers=headers,
        )
        user_response.raise_for_status()
        user_payload = user_response.json()
        if not isinstance(user_payload, dict):
            raise ValueError("Discord user response must be an object")
        user_id = int(user_payload["id"])

        guilds_response = await request(
            "GET",
            f"{DISCORD_API_BASE}/users/@me/guilds",
            headers=headers,
        )
        guilds_response.raise_for_status()
        guilds_payload = guilds_response.json()
        if not isinstance(guilds_payload, list):
            raise ValueError("Discord guilds response must be a list")

        permissions = 0
        for guild in guilds_payload:
            if not isinstance(guild, dict):
                continue
            if int(guild.get("id") or 0) == int(guild_id):
                permissions = int(guild.get("permissions") or 0)
                break
        else:
            raise ValueError("Discord user is not in the requested guild")

        member_response = await request(
            "GET",
            f"{DISCORD_API_BASE}/users/@me/guilds/{int(guild_id)}/member",
            headers=headers,
        )
        member_response.raise_for_status()
        member_payload = member_response.json()
        if not isinstance(member_payload, dict):
            raise ValueError("Discord guild member response must be an object")
        roles = member_payload.get("roles") or []
        if not isinstance(roles, list):
            raise ValueError("Discord guild member roles must be a list")
        return DiscordOAuthMember(
            user_id=user_id,
            guild_id=int(guild_id),
            permissions=permissions,
            role_ids=tuple(int(role_id) for role_id in roles),
        )


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
    guild_id: int,
    expires_at: int,
) -> str:
    body = _b64encode_json(
        {
            "file_name": artifact.file_name,
            "requester_id": int(requester_id),
            "guild_id": int(guild_id),
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
            guild_id=int(payload["guild_id"]),
            expires_at=expires_at,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def build_discord_authorization_url(
    *,
    state: str,
) -> str:
    return f"{DISCORD_AUTHORIZATION_URL}&{urlencode({'state': state})}"


def has_authorized_discord_download_access(
    member: DiscordOAuthMember,
    *,
    patreon_role_ids: Iterable[int],
    tester_role_ids: Iterable[int],
) -> bool:
    if member.permissions & ADMINISTRATOR_PERMISSION:
        return True
    authorized_roles = {int(role_id) for role_id in patreon_role_ids}
    authorized_roles.update(int(role_id) for role_id in tester_role_ids)
    return any(role_id in authorized_roles for role_id in member.role_ids)
