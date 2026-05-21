import hashlib
import hmac
import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from bulmaai.services.http import request


PATREON_API_BASE = "https://www.patreon.com/api/oauth2/v2"
PATREON_AUTHORIZE_URL = "https://www.patreon.com/oauth2/authorize"
PATREON_TOKEN_URL = "https://www.patreon.com/api/oauth2/token"
PATREON_OAUTH_SCOPE = "identity identity.memberships"


@dataclass(frozen=True, slots=True)
class PatreonOAuthState:
    discord_user_id: int
    guild_id: int
    action: str
    expires_at: int
    minecraft_username: str | None = None


@dataclass(frozen=True, slots=True)
class PatreonMemberStatus:
    patreon_user_id: str
    member_id: str | None
    full_name: str | None
    patron_status: str | None
    tier_ids: tuple[str, ...]
    last_charge_date: datetime | None


@dataclass(frozen=True, slots=True)
class PatreonIdentity:
    access_token: str
    status: PatreonMemberStatus


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


def build_patreon_oauth_state(
    *,
    secret: str,
    discord_user_id: int,
    guild_id: int,
    action: str,
    expires_at: int,
    minecraft_username: str | None = None,
) -> str:
    payload = {
        "discord_user_id": int(discord_user_id),
        "guild_id": int(guild_id),
        "action": str(action),
        "expires_at": int(expires_at),
    }
    if minecraft_username is not None:
        payload["minecraft_username"] = str(minecraft_username)
    body = _b64encode_json(payload)
    return f"{body}.{_sign_state(secret, body)}"


def parse_patreon_oauth_state(
    secret: str,
    state: str,
    *,
    now: Callable[[], float],
) -> PatreonOAuthState | None:
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
        return PatreonOAuthState(
            discord_user_id=int(payload["discord_user_id"]),
            guild_id=int(payload["guild_id"]),
            action=str(payload["action"]),
            expires_at=expires_at,
            minecraft_username=(
                str(payload["minecraft_username"])
                if payload.get("minecraft_username") is not None
                else None
            ),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def build_patreon_authorization_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": PATREON_OAUTH_SCOPE,
            "state": state,
        }
    )
    return f"{PATREON_AUTHORIZE_URL}?{query}"


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _relationship_ids(resource: Mapping[str, Any], relationship_name: str) -> tuple[str, ...]:
    relationship = ((resource.get("relationships") or {}).get(relationship_name) or {})
    data = relationship.get("data")
    if data is None:
        return tuple()
    if isinstance(data, list):
        return tuple(str(item["id"]) for item in data if isinstance(item, dict) and "id" in item)
    if isinstance(data, dict) and "id" in data:
        return (str(data["id"]),)
    return tuple()


def _resource_campaign_id(resource: Mapping[str, Any]) -> str | None:
    ids = _relationship_ids(resource, "campaign")
    return ids[0] if ids else None


def _find_membership(payload: Mapping[str, Any], *, campaign_id: str | None) -> Mapping[str, Any] | None:
    data = payload.get("data")
    membership_ids = set()
    if isinstance(data, dict):
        membership_ids.update(_relationship_ids(data, "memberships"))

    included = payload.get("included") or []
    if not isinstance(included, list):
        return None

    fallback: Mapping[str, Any] | None = None
    for resource in included:
        if not isinstance(resource, dict) or resource.get("type") != "member":
            continue
        resource_id = str(resource.get("id") or "")
        if membership_ids and resource_id not in membership_ids:
            continue
        if campaign_id and _resource_campaign_id(resource) != str(campaign_id):
            continue
        if fallback is None:
            fallback = resource
        if resource_id in membership_ids:
            return resource
    return fallback


def parse_patreon_member_status(
    payload: Mapping[str, Any],
    *,
    campaign_id: str | None,
) -> PatreonMemberStatus:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("Patreon identity response must contain a data object")

    member = _find_membership(payload, campaign_id=campaign_id)
    member_attrs = (member.get("attributes") if member is not None else {}) or {}
    user_attrs = data.get("attributes") or {}
    tier_ids = _relationship_ids(member, "currently_entitled_tiers") if member is not None else tuple()

    return PatreonMemberStatus(
        patreon_user_id=str(data["id"]),
        member_id=str(member["id"]) if member is not None and "id" in member else None,
        full_name=member_attrs.get("full_name") or user_attrs.get("full_name"),
        patron_status=member_attrs.get("patron_status"),
        tier_ids=tier_ids,
        last_charge_date=_parse_datetime(member_attrs.get("last_charge_date")),
    )


def is_active_entitled_patron(
    status: PatreonMemberStatus,
    *,
    eligible_tier_ids: tuple[str, ...],
) -> bool:
    if status.patron_status != "active_patron":
        return False
    eligible = {str(tier_id) for tier_id in eligible_tier_ids}
    return any(tier_id in eligible for tier_id in status.tier_ids)


def verify_patreon_webhook_signature(
    body: bytes,
    headers: Mapping[str, str],
    secret: str | None,
) -> bool:
    if not secret:
        return False
    provided = headers.get("X-Patreon-Signature") or headers.get("x-patreon-signature")
    if not provided:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.md5).hexdigest()
    return hmac.compare_digest(provided, expected)


class PatreonOAuthClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        campaign_id: str | None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.campaign_id = campaign_id

    async def fetch_identity_for_code(self, code: str) -> PatreonIdentity:
        token_response = await request(
            "POST",
            PATREON_TOKEN_URL,
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
        return await self.fetch_identity_for_access_token(access_token)

    async def fetch_identity_for_access_token(self, access_token: str) -> PatreonIdentity:
        response = await request(
            "GET",
            f"{PATREON_API_BASE}/identity",
            headers={"Authorization": f"Bearer {access_token}"},
            params={
                "include": "memberships.currently_entitled_tiers,memberships.campaign",
                "fields[user]": "full_name",
                "fields[member]": "full_name,patron_status,last_charge_date",
                "fields[tier]": "title",
                "fields[campaign]": "creation_name",
            },
        )
        response.raise_for_status()
        return PatreonIdentity(
            access_token=access_token,
            status=parse_patreon_member_status(response.json(), campaign_id=self.campaign_id),
        )


class PatreonCreatorClient:
    def __init__(self, *, creator_token: str, campaign_id: str | None):
        self.creator_token = creator_token
        self.campaign_id = campaign_id

    async def fetch_member_status(self, member_id: str) -> PatreonMemberStatus:
        response = await request(
            "GET",
            f"{PATREON_API_BASE}/members/{member_id}",
            headers={"Authorization": f"Bearer {self.creator_token}"},
            params={
                "include": "currently_entitled_tiers,campaign,user",
                "fields[member]": "full_name,patron_status,last_charge_date",
                "fields[user]": "full_name",
                "fields[tier]": "title",
                "fields[campaign]": "creation_name",
            },
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data")
        if isinstance(data, dict) and data.get("type") == "member":
            user_id = None
            user_ids = _relationship_ids(data, "user")
            if user_ids:
                user_id = user_ids[0]
            if user_id is not None:
                payload = {
                    "data": {"id": user_id, "type": "user", "relationships": {"memberships": {"data": [data]}}},
                    "included": [data],
                }
        return parse_patreon_member_status(payload, campaign_id=self.campaign_id)
