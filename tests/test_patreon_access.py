import hashlib
import hmac
import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch


os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.services.patreon_access import (
    PatreonIdentity,
    PatreonMemberStatus,
    build_patreon_authorization_url,
    build_patreon_oauth_state,
    is_active_entitled_patron,
    parse_patreon_member_status,
    parse_patreon_oauth_state,
    verify_patreon_webhook_signature,
)
from bulmaai.services.patreon_grants import (
    PatreonGrant,
    PatreonGrantKind,
    PatreonLink,
    count_active_gifts_for_owner,
    list_active_grants_for_owner,
    upsert_patreon_link,
    upsert_whitelist_grant,
)


class PatreonAccessTests(unittest.TestCase):
    def test_oauth_state_rejects_tampering_and_expiry(self) -> None:
        state = build_patreon_oauth_state(
            secret="secret",
            discord_user_id=456,
            guild_id=789,
            action="link",
            expires_at=2000,
        )

        parsed = parse_patreon_oauth_state("secret", state, now=lambda: 1999)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.discord_user_id, 456)
        self.assertEqual(parsed.guild_id, 789)
        self.assertEqual(parsed.action, "link")
        self.assertIsNone(parse_patreon_oauth_state("secret", state + "x", now=lambda: 1999))
        self.assertIsNone(parse_patreon_oauth_state("secret", state, now=lambda: 2001))

    def test_authorization_url_uses_identity_memberships_scope(self) -> None:
        url = build_patreon_authorization_url(
            client_id="client-id",
            redirect_uri="https://downloads.example.test/patreon/oauth/callback",
            state="signed-state",
        )

        self.assertIn("https://www.patreon.com/oauth2/authorize?", url)
        self.assertIn("client_id=client-id", url)
        self.assertIn("redirect_uri=https%3A%2F%2Fdownloads.example.test%2Fpatreon%2Foauth%2Fcallback", url)
        self.assertIn("scope=identity+identity.memberships", url)
        self.assertIn("state=signed-state", url)

    def test_parse_member_status_reads_identity_membership_tiers(self) -> None:
        payload = {
            "data": {
                "id": "patreon-user-1",
                "attributes": {"full_name": "Patron User"},
                "relationships": {"memberships": {"data": [{"id": "member-1", "type": "member"}]}},
            },
            "included": [
                {
                    "id": "member-1",
                    "type": "member",
                    "attributes": {
                        "full_name": "Patron User",
                        "patron_status": "active_patron",
                        "last_charge_date": "2026-05-01T00:00:00.000+00:00",
                    },
                    "relationships": {
                        "campaign": {"data": {"id": "12861895", "type": "campaign"}},
                        "currently_entitled_tiers": {
                            "data": [{"id": "1287877305259130900", "type": "tier"}]
                        },
                    },
                }
            ],
        }

        status = parse_patreon_member_status(payload, campaign_id="12861895")

        self.assertEqual(
            status,
            PatreonMemberStatus(
                patreon_user_id="patreon-user-1",
                member_id="member-1",
                full_name="Patron User",
                patron_status="active_patron",
                tier_ids=("1287877305259130900",),
                last_charge_date=datetime(2026, 5, 1, tzinfo=timezone.utc),
            ),
        )
        self.assertTrue(
            is_active_entitled_patron(
                status,
                eligible_tier_ids=("1287877272224665640", "1287877305259130900"),
            )
        )

    def test_inactive_patron_with_tier_is_not_entitled(self) -> None:
        status = PatreonMemberStatus(
            patreon_user_id="patreon-user-1",
            member_id="member-1",
            full_name="Patron User",
            patron_status="declined_patron",
            tier_ids=("1287877305259130900",),
            last_charge_date=None,
        )

        self.assertFalse(
            is_active_entitled_patron(
                status,
                eligible_tier_ids=("1287877272224665640", "1287877305259130900"),
            )
        )

    def test_webhook_signature_uses_patreon_hmac_md5_header(self) -> None:
        body = json.dumps({"data": {"id": "member-1"}}).encode("utf-8")
        signature = hmac.new(b"webhook-secret", body, hashlib.md5).hexdigest()

        self.assertTrue(
            verify_patreon_webhook_signature(
                body,
                {"X-Patreon-Signature": signature},
                "webhook-secret",
            )
        )
        self.assertFalse(
            verify_patreon_webhook_signature(
                body,
                {"X-Patreon-Signature": "wrong"},
                "webhook-secret",
            )
        )


class PatreonGrantStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_link_and_grant_upserts_persist_discord_and_patreon_identity(self) -> None:
        conn = AsyncMock()
        pool = MagicMock()
        pool.acquire.return_value.__aenter__.return_value = conn

        with patch("bulmaai.services.patreon_grants.get_pool", AsyncMock(return_value=pool)):
            await upsert_patreon_link(
                PatreonLink(
                    discord_user_id=456,
                    discord_username="Requester",
                    patreon_user_id="patreon-user-1",
                    patreon_member_id="member-1",
                    patreon_full_name="Patron User",
                    patron_status="active_patron",
                    tier_ids=("1287877272224665640",),
                    last_charge_date=None,
                    entitlement_active=True,
                )
            )
            await upsert_whitelist_grant(
                PatreonGrant(
                    owner_discord_user_id=456,
                    beneficiary_discord_user_id=789,
                    beneficiary_discord_username="Gifted",
                    minecraft_username="GiftedMC",
                    kind=PatreonGrantKind.GIFT,
                    active=True,
                    source_pr_url="https://example.test/pr/1",
                )
            )

        self.assertEqual(conn.execute.await_count, 2)
        self.assertIn("INSERT INTO patreon_links", conn.execute.await_args_list[0].args[0])
        self.assertIn("INSERT INTO patreon_whitelist_grants", conn.execute.await_args_list[1].args[0])

    async def test_grant_reads_map_rows_to_dataclasses(self) -> None:
        conn = AsyncMock()
        conn.fetchval.return_value = 2
        conn.fetch.return_value = [
            {
                "owner_discord_user_id": 456,
                "beneficiary_discord_user_id": 456,
                "beneficiary_discord_username": "Requester",
                "minecraft_username": "OwnerMC",
                "kind": "self",
                "active": True,
                "source_pr_url": "https://example.test/pr/2",
            },
            {
                "owner_discord_user_id": 456,
                "beneficiary_discord_user_id": 789,
                "beneficiary_discord_username": "Gifted",
                "minecraft_username": "GiftedMC",
                "kind": "gift",
                "active": True,
                "source_pr_url": None,
            },
        ]
        pool = MagicMock()
        pool.acquire.return_value.__aenter__.return_value = conn

        with patch("bulmaai.services.patreon_grants.get_pool", AsyncMock(return_value=pool)):
            self.assertEqual(await count_active_gifts_for_owner(456), 2)
            grants = await list_active_grants_for_owner(456)

        self.assertEqual(len(grants), 2)
        self.assertEqual(grants[0].kind, PatreonGrantKind.SELF)
        self.assertEqual(grants[1].minecraft_username, "GiftedMC")


if __name__ == "__main__":
    unittest.main()
