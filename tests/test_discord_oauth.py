import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlparse

from bulmaai.services.discord_oauth import (
    DiscordOAuthClient,
    build_discord_authorization_url,
    build_discord_oauth_state,
    parse_discord_oauth_state,
)


class DiscordOAuthTests(unittest.IsolatedAsyncioTestCase):
    def test_oauth_state_rejects_tampering_and_expiry(self) -> None:
        state = build_discord_oauth_state(
            secret="secret",
            minecraft_username="NewTester",
            expires_at=2000,
        )

        parsed = parse_discord_oauth_state("secret", state, now=lambda: 1999)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.minecraft_username, "NewTester")
        self.assertEqual(parsed.expires_at, 2000)
        self.assertIsNone(parse_discord_oauth_state("secret", state + "x", now=lambda: 1999))
        self.assertIsNone(parse_discord_oauth_state("secret", state, now=lambda: 2001))

    def test_authorization_url_uses_identify_scope_and_redirect_uri(self) -> None:
        url = build_discord_authorization_url(
            client_id="discord-client-id",
            redirect_uri="https://downloads.example.test/beta-access/discord/callback",
            state="signed-state",
        )
        parsed = urlparse(url)
        query = parse_qs(parsed.query)

        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "discord.com")
        self.assertEqual(parsed.path, "/oauth2/authorize")
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["client_id"], ["discord-client-id"])
        self.assertEqual(query["redirect_uri"], ["https://downloads.example.test/beta-access/discord/callback"])
        self.assertEqual(query["scope"], ["identify"])
        self.assertEqual(query["state"], ["signed-state"])

    async def test_client_fetches_discord_user_id_for_authorization_code(self) -> None:
        token_response = Mock()
        token_response.json.return_value = {"access_token": "discord-access-token"}
        token_response.raise_for_status.return_value = None
        identity_response = Mock()
        identity_response.json.return_value = {"id": "456"}
        identity_response.raise_for_status.return_value = None

        with patch(
            "bulmaai.services.discord_oauth.request",
            AsyncMock(side_effect=[token_response, identity_response]),
        ) as request:
            client = DiscordOAuthClient(
                client_id="discord-client-id",
                client_secret="discord-client-secret",
                redirect_uri="https://downloads.example.test/beta-access/discord/callback",
            )

            user_id = await client.fetch_user_id_for_code("oauth-code")

        self.assertEqual(user_id, 456)
        token_call = request.await_args_list[0]
        self.assertEqual(token_call.args[:2], ("POST", "https://discord.com/api/oauth2/token"))
        self.assertEqual(token_call.kwargs["data"]["grant_type"], "authorization_code")
        self.assertEqual(token_call.kwargs["data"]["code"], "oauth-code")
        identity_call = request.await_args_list[1]
        self.assertEqual(identity_call.args[:2], ("GET", "https://discord.com/api/users/@me"))
        self.assertEqual(identity_call.kwargs["headers"]["Authorization"], "Bearer discord-access-token")


if __name__ == "__main__":
    unittest.main()
