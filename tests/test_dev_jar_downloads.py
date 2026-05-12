import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bulmaai.cogs.dev_jar_downloads import (
    DevJarDownloadsCog,
    build_dev_jar_download_embed,
    can_bypass_patreon_oauth,
)
from bulmaai.services.dev_jar_downloads import (
    DevJarUploadPayload,
    OneTimeDownloadTokenStore,
    PatreonOAuthClient,
    build_oauth_state,
    build_patreon_authorization_url,
    find_latest_dev_jar,
    has_active_patreon_membership,
    parse_dev_jar_filename,
    parse_oauth_state,
)


class DevJarDownloadsTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_dev_jar_filename_keeps_hyphenated_version_intact(self) -> None:
        artifact = parse_dev_jar_filename(
            "dragonminez-2.1.2-alpha__v2.1__39cd4f1c1234.jar"
        )

        self.assertEqual(artifact.version, "2.1.2-alpha")
        self.assertEqual(artifact.branch_slug, "v2.1")
        self.assertEqual(artifact.commit_sha, "39cd4f1c1234")

    def test_parse_dev_jar_filename_rejects_path_like_names(self) -> None:
        with self.assertRaises(ValueError):
            parse_dev_jar_filename("../dragonminez-2.1.2__v2.1__39cd4f1c1234.jar")

    def test_find_latest_dev_jar_uses_file_modified_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            older = upload_dir / "dragonminez-2.1.1__v2.1__111111111111.jar"
            newer = upload_dir / "dragonminez-2.1.2__v2.1__222222222222.jar"
            ignored = upload_dir / "dragonminez-2.1.2-slim.jar"
            older.write_bytes(b"older")
            newer.write_bytes(b"newer")
            ignored.write_bytes(b"ignored")
            older.touch()
            newer.touch()

            artifact = find_latest_dev_jar(upload_dir)

        self.assertEqual(artifact.file_name, newer.name)
        self.assertEqual(artifact.version, "2.1.2")

    def test_one_time_download_token_can_only_be_consumed_once(self) -> None:
        now = 1000.0
        store = OneTimeDownloadTokenStore(now=lambda: now)
        artifact = parse_dev_jar_filename("dragonminez-2.1.2__v2.1__222222222222.jar")

        token = store.issue(artifact=artifact, requester_id=123, ttl_seconds=60)

        first = store.consume(token)
        second = store.consume(token)

        self.assertEqual(first, artifact)
        self.assertIsNone(second)

    def test_one_time_download_token_claim_completes_or_releases(self) -> None:
        store = OneTimeDownloadTokenStore(now=lambda: 1000)
        artifact = parse_dev_jar_filename("dragonminez-2.1.2__v2.1__222222222222.jar")
        token = store.issue(artifact=artifact, requester_id=123, ttl_seconds=60)

        claim = store.claim(token)
        self.assertIsNotNone(claim)
        self.assertIsNone(store.claim(token))
        assert claim is not None
        store.release_claim(claim)

        retry_claim = store.claim(token)
        self.assertIsNotNone(retry_claim)
        assert retry_claim is not None
        self.assertEqual(retry_claim.artifact, artifact)
        store.complete_claim(retry_claim)

        self.assertIsNone(store.claim(token))

    def test_one_time_download_token_expires(self) -> None:
        now = 1000.0

        def current_time() -> float:
            return now

        store = OneTimeDownloadTokenStore(now=current_time)
        artifact = parse_dev_jar_filename("dragonminez-2.1.2__v2.1__222222222222.jar")
        token = store.issue(artifact=artifact, requester_id=123, ttl_seconds=5)
        now = 1006.0

        self.assertIsNone(store.consume(token))

    def test_active_patreon_membership_is_accepted(self) -> None:
        payload = {
            "included": [
                {
                    "type": "member",
                    "id": "member-1",
                    "attributes": {
                        "patron_status": "active_patron",
                        "last_charge_status": "Paid",
                        "currently_entitled_amount_cents": 999,
                    },
                }
            ]
        }

        self.assertTrue(has_active_patreon_membership(payload))

    def test_active_patreon_membership_must_match_campaign_when_present(self) -> None:
        payload = {
            "included": [
                {
                    "type": "member",
                    "id": "member-1",
                    "relationships": {
                        "campaign": {"data": {"type": "campaign", "id": "12861895"}}
                    },
                    "attributes": {
                        "patron_status": "active_patron",
                        "last_charge_status": "Paid",
                        "currently_entitled_amount_cents": 999,
                    },
                }
            ]
        }

        self.assertTrue(has_active_patreon_membership(payload, campaign_id="12861895"))
        self.assertFalse(has_active_patreon_membership(payload, campaign_id="other"))

    def test_declined_patreon_membership_is_rejected(self) -> None:
        payload = {
            "included": [
                {
                    "type": "member",
                    "id": "member-1",
                    "attributes": {
                        "patron_status": "declined_patron",
                        "last_charge_status": "Declined",
                        "currently_entitled_amount_cents": 999,
                    },
                }
            ]
        }

        self.assertFalse(has_active_patreon_membership(payload))

    def test_oauth_state_round_trips_artifact_and_discord_user(self) -> None:
        artifact = parse_dev_jar_filename("dragonminez-2.1.2__v2.1__222222222222.jar")

        state = build_oauth_state(
            secret="secret",
            artifact=artifact,
            requester_id=123,
            expires_at=2000,
        )
        parsed = parse_oauth_state("secret", state, now=lambda: 1999)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.artifact, artifact)
        self.assertEqual(parsed.requester_id, 123)

    def test_oauth_state_rejects_tampering_and_expiry(self) -> None:
        artifact = parse_dev_jar_filename("dragonminez-2.1.2__v2.1__222222222222.jar")
        state = build_oauth_state(
            secret="secret",
            artifact=artifact,
            requester_id=123,
            expires_at=2000,
        )

        self.assertIsNone(parse_oauth_state("secret", state + "x", now=lambda: 1999))
        self.assertIsNone(parse_oauth_state("secret", state, now=lambda: 2001))

    def test_build_patreon_authorization_url_uses_membership_scope(self) -> None:
        url = build_patreon_authorization_url(
            client_id="client-id",
            redirect_uri="https://downloads.example.test/dev-download/oauth/callback",
            state="state-token",
        )

        self.assertIn("https://www.patreon.com/oauth2/authorize?", url)
        self.assertIn("client_id=client-id", url)
        self.assertIn("response_type=code", url)
        self.assertIn("scope=identity+identity.memberships", url)
        self.assertIn("state=state-token", url)

    def test_staff_role_or_admin_can_bypass_patreon_oauth(self) -> None:
        staff_member = SimpleNamespace(
            guild_permissions=SimpleNamespace(administrator=False),
            roles=[SimpleNamespace(id=1341596685339725885)],
        )
        admin_member = SimpleNamespace(
            guild_permissions=SimpleNamespace(administrator=True),
            roles=[],
        )
        regular_member = SimpleNamespace(
            guild_permissions=SimpleNamespace(administrator=False),
            roles=[],
        )

        self.assertTrue(
            can_bypass_patreon_oauth(
                staff_member,
                bypass_role_ids=(1341596685339725885,),
            )
        )
        self.assertTrue(
            can_bypass_patreon_oauth(
                admin_member,
                bypass_role_ids=(1341596685339725885,),
            )
        )
        self.assertFalse(
            can_bypass_patreon_oauth(
                regular_member,
                bypass_role_ids=(1341596685339725885,),
            )
        )

    def test_download_embed_mentions_commit_and_workflow(self) -> None:
        artifact = parse_dev_jar_filename("dragonminez-2.1.2__v2.1__222222222222.jar")

        embed = build_dev_jar_download_embed(
            artifact,
            sha256="a" * 64,
            workflow_run_url="https://github.com/DragonMineZ/dragonminez/actions/runs/123",
        )

        self.assertEqual(embed.title, "DragonMineZ Dev jar")
        self.assertEqual(embed.url, "https://github.com/DragonMineZ/dragonminez/actions/runs/123")
        field_values = [field.value for field in embed.fields]
        self.assertIn("`222222222222`", field_values)
        self.assertIn("`v2.1`", field_values)

    def test_cog_direct_token_download_consumes_token_after_successful_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            artifact = parse_dev_jar_filename("dragonminez-2.1.2__v2.1__222222222222.jar")
            (upload_dir / artifact.file_name).write_bytes(b"jar")
            cog = DevJarDownloadsCog.__new__(DevJarDownloadsCog)
            cog.settings = SimpleNamespace(dev_jar_download_upload_dir=str(upload_dir))
            cog.token_store = OneTimeDownloadTokenStore(now=lambda: 1000)
            token = cog.token_store.issue(
                artifact=artifact,
                requester_id=123,
                ttl_seconds=60,
            )

            first = cog._handle_direct_token(token)
            second = cog._handle_direct_token(token)
            assert first.on_stream_complete is not None
            first.on_stream_complete()
            third = cog._handle_direct_token(token)

        self.assertEqual(first.status, 200)
        self.assertEqual(first.download_name, artifact.file_name)
        self.assertEqual(second.status, 403)
        self.assertEqual(third.status, 403)

    def test_cog_direct_token_download_can_retry_after_interrupted_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            artifact = parse_dev_jar_filename("dragonminez-2.1.2__v2.1__222222222222.jar")
            (upload_dir / artifact.file_name).write_bytes(b"jar")
            cog = DevJarDownloadsCog.__new__(DevJarDownloadsCog)
            cog.settings = SimpleNamespace(dev_jar_download_upload_dir=str(upload_dir))
            cog.token_store = OneTimeDownloadTokenStore(now=lambda: 1000)
            token = cog.token_store.issue(
                artifact=artifact,
                requester_id=123,
                ttl_seconds=60,
            )

            first = cog._handle_direct_token(token)
            assert first.on_stream_error is not None
            first.on_stream_error(ConnectionResetError("client reset"))
            retry = cog._handle_direct_token(token)

        self.assertEqual(first.status, 200)
        self.assertEqual(retry.status, 200)

    async def test_cog_oauth_callback_streams_file_for_active_patron(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            artifact = parse_dev_jar_filename("dragonminez-2.1.2__v2.1__222222222222.jar")
            (upload_dir / artifact.file_name).write_bytes(b"jar")
            cog = DevJarDownloadsCog.__new__(DevJarDownloadsCog)
            cog.settings = SimpleNamespace(
                dev_jar_download_upload_dir=str(upload_dir),
                release_webhook_secret="secret",
                dev_jar_download_public_base_url="https://downloads.example.test",
                dev_jar_download_oauth_callback_path="/dev-download/oauth/callback",
                patreon_oauth_redirect_uri=None,
                patreon_oauth_client_id="client-id",
                patreon_oauth_client_secret="client-secret",
                PATREON_CAMPAIGN_ID=None,
            )
            state = build_oauth_state(
                secret="secret",
                artifact=artifact,
                requester_id=123,
                expires_at=2000,
            )
            identity_payload = {
                "included": [
                    {
                        "type": "member",
                        "attributes": {
                            "patron_status": "active_patron",
                            "last_charge_status": "Paid",
                            "currently_entitled_amount_cents": 999,
                        },
                    }
                ]
            }

            with patch(
                "bulmaai.cogs.dev_jar_downloads.PatreonOAuthClient.fetch_identity_for_code",
                AsyncMock(return_value=identity_payload),
            ), patch("bulmaai.cogs.dev_jar_downloads.time.time", return_value=1999):
                response = await cog._handle_oauth_callback("oauth-code", state)

        self.assertEqual(response.status, 200)
        self.assertEqual(response.download_name, artifact.file_name)

    async def test_cog_upload_payload_posts_download_announcement(self) -> None:
        class FakeChannel:
            def __init__(self) -> None:
                self.sent: list[dict] = []

            async def send(self, **kwargs) -> None:
                self.sent.append(kwargs)

        class FakeBot:
            def __init__(self, channel: FakeChannel) -> None:
                self._channel = channel

            def get_channel(self, channel_id: int) -> FakeChannel:
                return self._channel

        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            artifact = parse_dev_jar_filename("dragonminez-2.1.2__v2.1__222222222222.jar")
            (upload_dir / artifact.file_name).write_bytes(b"jar")
            channel = FakeChannel()
            cog = DevJarDownloadsCog.__new__(DevJarDownloadsCog)
            cog.bot = FakeBot(channel)
            cog.settings = SimpleNamespace(
                dev_jar_download_upload_dir=str(upload_dir),
                dev_jar_download_channel_id=123,
            )

            await cog._handle_upload_payload(
                DevJarUploadPayload(
                    artifact=artifact,
                    sha256="a" * 64,
                    workflow_run_url="https://github.com/DragonMineZ/dragonminez/actions/runs/123",
                )
            )

        self.assertEqual(len(channel.sent), 1)
        embed = channel.sent[0]["embed"]
        self.assertEqual(embed.url, "https://github.com/DragonMineZ/dragonminez/actions/runs/123")
        field_values = {field.name: field.value for field in embed.fields}
        self.assertEqual(field_values["Artifact"], f"`{artifact.file_name}`")
        self.assertEqual(field_values["Size"], "0.0 MB")
        self.assertEqual(field_values["SHA-256"], f"`{'a' * 64}`")

    async def test_patreon_oauth_client_fetches_identity_memberships(self) -> None:
        class FakeResponse:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        calls = AsyncMock(
            side_effect=[
                FakeResponse({"access_token": "patreon-access-token"}),
                FakeResponse({"included": []}),
            ]
        )
        client = PatreonOAuthClient(
            client_id="client-id",
            client_secret="client-secret",
            redirect_uri="https://downloads.example.test/dev-download/oauth/callback",
        )

        with patch("bulmaai.services.dev_jar_downloads.request", calls):
            payload = await client.fetch_identity_for_code("oauth-code")

        self.assertEqual(payload, {"included": []})
        token_call = calls.await_args_list[0].kwargs
        identity_call = calls.await_args_list[1].kwargs
        self.assertEqual(token_call["data"]["grant_type"], "authorization_code")
        self.assertEqual(token_call["data"]["code"], "oauth-code")
        self.assertEqual(identity_call["headers"]["Authorization"], "Bearer patreon-access-token")
        self.assertEqual(identity_call["params"]["include"], "memberships,memberships.campaign")
        self.assertIn("patron_status", identity_call["params"]["fields[member]"])
        self.assertIn("creation_name", identity_call["params"]["fields[campaign]"])

if __name__ == "__main__":
    unittest.main()
