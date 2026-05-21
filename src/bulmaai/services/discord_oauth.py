import hashlib
import hmac
import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from bulmaai.services.http import request


DISCORD_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_USER_URL = "https://discord.com/api/users/@me"
DISCORD_OAUTH_SCOPE = "identify"


@dataclass(frozen=True, slots=True)
class DiscordOAuthState:
    minecraft_username: str
    expires_at: int


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


def build_discord_oauth_state(
    *,
    secret: str,
    minecraft_username: str,
    expires_at: int,
) -> str:
    body = _b64encode_json(
        {
            "minecraft_username": str(minecraft_username),
            "expires_at": int(expires_at),
        }
    )
    return f"{body}.{_sign_state(secret, body)}"


def parse_discord_oauth_state(
    secret: str,
    state: str,
    *,
    now: Callable[[], float],
) -> DiscordOAuthState | None:
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
        return DiscordOAuthState(
            minecraft_username=str(payload["minecraft_username"]),
            expires_at=expires_at,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def build_discord_authorization_url(
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
            "scope": DISCORD_OAUTH_SCOPE,
            "state": state,
        }
    )
    return f"{DISCORD_AUTHORIZE_URL}?{query}"


class DiscordOAuthClient:
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

    async def fetch_user_id_for_code(self, code: str) -> int:
        token_response = await request(
            "POST",
            DISCORD_TOKEN_URL,
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
            DISCORD_USER_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        identity_response.raise_for_status()
        return int(identity_response.json()["id"])
