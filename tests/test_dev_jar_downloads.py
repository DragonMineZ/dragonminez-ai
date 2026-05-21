import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bulmaai.cogs.dev_jar_downloads import (
    DevJarDownloadsCog,
    build_dev_jar_download_embed,
)
from bulmaai.services.dev_jar_downloads import (
    ADMINISTRATOR_PERMISSION,
    DevJarCommit,
    DevJarUploadPayload,
    DiscordOAuthMember,
    DiscordOAuthClient,
    OneTimeDownloadTokenStore,
    build_oauth_state,
    build_discord_authorization_url,
    find_latest_dev_jar,
    has_authorized_discord_download_access,
    parse_dev_jar_upload_payload,
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

    def test_discord_download_access_allows_admin_patreon_or_tester_role(self) -> None:
        patreon_role_ids = (1287877272224665640, 1287877305259130900)
        tester_role_ids = (1286814599215317034,)

        self.assertTrue(
            has_authorized_discord_download_access(
                DiscordOAuthMember(
                    user_id=123,
                    guild_id=456,
                    permissions=ADMINISTRATOR_PERMISSION,
                    role_ids=(),
                ),
                patreon_role_ids=patreon_role_ids,
                tester_role_ids=tester_role_ids,
            )
        )
        self.assertTrue(
            has_authorized_discord_download_access(
                DiscordOAuthMember(
                    user_id=123,
                    guild_id=456,
                    permissions=0,
                    role_ids=(1287877272224665640,),
                ),
                patreon_role_ids=patreon_role_ids,
                tester_role_ids=tester_role_ids,
            )
        )
        self.assertTrue(
            has_authorized_discord_download_access(
                DiscordOAuthMember(
                    user_id=123,
                    guild_id=456,
                    permissions=0,
                    role_ids=(1286814599215317034,),
                ),
                patreon_role_ids=patreon_role_ids,
                tester_role_ids=tester_role_ids,
            )
        )
        self.assertFalse(
            has_authorized_discord_download_access(
                DiscordOAuthMember(
                    user_id=123,
                    guild_id=456,
                    permissions=0,
                    role_ids=(999,),
                ),
                patreon_role_ids=patreon_role_ids,
                tester_role_ids=tester_role_ids,
            )
        )

    def test_oauth_state_round_trips_artifact_and_discord_user(self) -> None:
        artifact = parse_dev_jar_filename("dragonminez-2.1.2__v2.1__222222222222.jar")

        state = build_oauth_state(
            secret="secret",
            artifact=artifact,
            requester_id=123,
            guild_id=456,
            expires_at=2000,
        )
        parsed = parse_oauth_state("secret", state, now=lambda: 1999)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.artifact, artifact)
        self.assertEqual(parsed.requester_id, 123)
        self.assertEqual(parsed.guild_id, 456)

    def test_oauth_state_rejects_tampering_and_expiry(self) -> None:
        artifact = parse_dev_jar_filename("dragonminez-2.1.2__v2.1__222222222222.jar")
        state = build_oauth_state(
            secret="secret",
            artifact=artifact,
            requester_id=123,
            guild_id=456,
            expires_at=2000,
        )

        self.assertIsNone(parse_oauth_state("secret", state + "x", now=lambda: 1999))
        self.assertIsNone(parse_oauth_state("secret", state, now=lambda: 2001))

    def test_build_discord_authorization_url_uses_hardcoded_production_oauth_url(self) -> None:
        url = build_discord_authorization_url(state="state-token")

        self.assertTrue(
            url.startswith(
                "https://discord.com/oauth2/authorize?"
                "client_id=1336867824815312906&response_type=code&"
                "redirect_uri=https%3A%2F%2Fdownloads.dragonminez.com%2Fdiscord%2Foauth%2Fcallback&"
                "scope=identify+guilds.members.read+guilds"
            )
        )
        self.assertIn("response_type=code", url)
        self.assertIn("state=state-token", url)

    def test_parse_dev_jar_upload_payload_requires_commit_metadata(self) -> None:
        with self.assertRaisesRegex(ValueError, "commits"):
            parse_dev_jar_upload_payload(
                {
                    "remote_name": "dragonminez-2.1.2__v2.1__222222222222.jar",
                    "sha256": "a" * 64,
                }
            )

    def test_parse_dev_jar_upload_payload_accepts_required_commit_fields(self) -> None:
        payload = parse_dev_jar_upload_payload(
            {
                "remote_name": "dragonminez-2.1.2__v2.1__222222222222.jar",
                "sha256": "a" * 64,
                "workflow_run_url": "https://github.com/DragonMineZ/dragonminez/actions/runs/123",
                "commits": [
                    {
                        "sha": "93066058a79b",
                        "title": "feat: changed form drains",
                        "description": "Adds support for new drain behavior.",
                        "author": "Shokkoh",
                        "url": "https://github.com/DragonMineZ/dragonminez/commit/93066058a79b",
                    },
                    {
                        "sha": "086afb963f2c",
                        "title": "fix: race selection screen fix",
                        "author": "Shokkoh",
                        "url": "https://github.com/DragonMineZ/dragonminez/commit/086afb963f2c",
                    },
                ],
            }
        )

        self.assertEqual(len(payload.commits), 2)
        self.assertEqual(payload.commits[0].description, "Adds support for new drain behavior.")
        self.assertIsNone(payload.commits[1].description)
        self.assertEqual(payload.commits[1].author, "Shokkoh")

    def test_download_embed_mentions_commit_and_workflow(self) -> None:
        artifact = parse_dev_jar_filename("dragonminez-2.1.2__v2.1__222222222222.jar")

        embed = build_dev_jar_download_embed(
            artifact,
            sha256="a" * 64,
            workflow_run_url="https://github.com/DragonMineZ/dragonminez/actions/runs/123",
            commits=(
                DevJarCommit(
                    sha="222222222222",
                    title="fix: race selection screen fix",
                    description=None,
                    author="Shokkoh",
                    url="https://github.com/DragonMineZ/dragonminez/commit/222222222222",
                ),
            ),
        )

        self.assertEqual(embed.title, "DragonMineZ Dev jar")
        self.assertEqual(embed.url, "https://github.com/DragonMineZ/dragonminez/actions/runs/123")
        field_values = [field.value for field in embed.fields]
        self.assertIn("`222222222222`", field_values)
        self.assertIn("`v2.1`", field_values)

    def test_download_embed_includes_commit_summary_links_titles_descriptions_and_authors(self) -> None:
        artifact = parse_dev_jar_filename("dragonminez-2.1.2__v2.1__086afb963f2c.jar")

        embed = build_dev_jar_download_embed(
            artifact,
            commits=(
                DevJarCommit(
                    sha="93066058a79b",
                    title="feat: changed form drains",
                    description="Adds support for new drain behavior.",
                    author="Shokkoh",
                    url="https://github.com/DragonMineZ/dragonminez/commit/93066058a79b",
                ),
                DevJarCommit(
                    sha="086afb963f2c",
                    title="fix: race selection screen fix",
                    description=None,
                    author="Shokkoh",
                    url="https://github.com/DragonMineZ/dragonminez/commit/086afb963f2c",
                ),
            ),
        )

        field_values = {field.name: field.value for field in embed.fields}
        self.assertIn("[9306605](https://github.com/DragonMineZ/dragonminez/commit/93066058a79b)", field_values["Commits"])
        self.assertIn("feat: changed form drains", field_values["Commits"])
        self.assertIn("Adds support for new drain behavior.", field_values["Commits"])
        self.assertIn("- Shokkoh", field_values["Commits"])
        self.assertIn("[086afb9](https://github.com/DragonMineZ/dragonminez/commit/086afb963f2c)", field_values["Commits"])
        self.assertIn("fix: race selection screen fix", field_values["Commits"])

    def test_cog_direct_token_download_consumes_token_after_successful_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            artifact = parse_dev_jar_filename("dragonminez-2.1.2__v2.1__222222222222.jar")
            (upload_dir / artifact.file_name).write_bytes(b"jar")
            cog = DevJarDownloadsCog.__new__(DevJarDownloadsCog)
            cog.settings = SimpleNamespace(
                dev_jar_download_upload_dir=str(upload_dir),
                dev_jar_download_public_base_url="https://downloads.example.test",
                dev_jar_download_download_path="/dev-download",
            )
            cog.token_store = OneTimeDownloadTokenStore(now=lambda: 1000)
            token = cog.token_store.issue(
                artifact=artifact,
                requester_id=123,
                ttl_seconds=60,
            )

            landing = cog._handle_direct_token(token)
            first_file = cog._handle_direct_token_file(token)
            second_file = cog._handle_direct_token_file(token)
            assert first_file.on_stream_complete is not None
            first_file.on_stream_complete()
            third_file = cog._handle_direct_token_file(token)

        self.assertEqual(landing.status, 200)
        self.assertEqual(landing.content_type, "text/html; charset=utf-8")
        self.assertIn(b"200 success", landing.body)
        self.assertIn(b"/dev-download/", landing.body)
        self.assertIn(b"/file", landing.body)
        self.assertEqual(first_file.status, 200)
        self.assertEqual(first_file.download_name, artifact.file_name)
        self.assertEqual(second_file.status, 403)
        self.assertEqual(third_file.status, 403)

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

            first = cog._handle_direct_token_file(token)
            assert first.on_stream_error is not None
            first.on_stream_error(ConnectionResetError("client reset"))
            retry = cog._handle_direct_token_file(token)

        self.assertEqual(first.status, 200)
        self.assertEqual(retry.status, 200)

    async def test_cog_oauth_callback_streams_file_for_authorized_tester(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            artifact = parse_dev_jar_filename("dragonminez-2.1.2__v2.1__222222222222.jar")
            (upload_dir / artifact.file_name).write_bytes(b"jar")
            cog = DevJarDownloadsCog.__new__(DevJarDownloadsCog)
            cog.settings = SimpleNamespace(
                dev_jar_download_upload_dir=str(upload_dir),
                release_webhook_secret="secret",
                dev_jar_download_public_base_url="https://downloads.example.test",
                dev_jar_download_download_path="/dev-download",
                discord_oauth_client_secret="client-secret",
            )
            cog.token_store = OneTimeDownloadTokenStore(now=lambda: 1999)
            state = build_oauth_state(
                secret="secret",
                artifact=artifact,
                requester_id=123,
                guild_id=456,
                expires_at=2000,
            )
            identity_payload = DiscordOAuthMember(
                user_id=123,
                guild_id=456,
                permissions=0,
                role_ids=(1286814599215317034,),
            )

            with patch(
                "bulmaai.cogs.dev_jar_downloads.DiscordOAuthClient.fetch_member_for_code",
                AsyncMock(return_value=identity_payload),
            ), patch("bulmaai.cogs.dev_jar_downloads.time.time", return_value=1999):
                response = await cog._handle_oauth_callback("oauth-code", state)

        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "text/html; charset=utf-8")
        self.assertIn(b"200 success", response.body)
        self.assertIn(b"/dev-download/", response.body)
        self.assertIn(b"/file", response.body)

    async def test_cog_oauth_callback_rejects_unapproved_discord_member(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            artifact = parse_dev_jar_filename("dragonminez-2.1.2__v2.1__222222222222.jar")
            (upload_dir / artifact.file_name).write_bytes(b"jar")
            cog = DevJarDownloadsCog.__new__(DevJarDownloadsCog)
            cog.settings = SimpleNamespace(
                dev_jar_download_upload_dir=str(upload_dir),
                release_webhook_secret="secret",
                dev_jar_download_public_base_url="https://downloads.example.test",
                dev_jar_download_download_path="/dev-download",
                discord_oauth_client_secret="client-secret",
            )
            cog.token_store = OneTimeDownloadTokenStore(now=lambda: 1999)
            state = build_oauth_state(
                secret="secret",
                artifact=artifact,
                requester_id=123,
                guild_id=456,
                expires_at=2000,
            )
            identity_payload = DiscordOAuthMember(
                user_id=123,
                guild_id=456,
                permissions=0,
                role_ids=(999,),
            )

            with patch(
                "bulmaai.cogs.dev_jar_downloads.DiscordOAuthClient.fetch_member_for_code",
                AsyncMock(return_value=identity_payload),
            ), patch("bulmaai.cogs.dev_jar_downloads.time.time", return_value=1999):
                response = await cog._handle_oauth_callback("oauth-code", state)

        self.assertEqual(response.status, 403)
        self.assertIn(b"Discord account is not authorized", response.body)

    async def test_download_button_sends_discord_oauth_for_admin_instead_of_direct_link(self) -> None:
        class FakeResponse:
            def __init__(self) -> None:
                self.messages: list[tuple[str, dict]] = []

            async def send_message(self, content: str, **kwargs) -> None:
                self.messages.append((content, kwargs))

        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            artifact = parse_dev_jar_filename("dragonminez-2.1.2__v2.1__222222222222.jar")
            (upload_dir / artifact.file_name).write_bytes(b"jar")
            cog = DevJarDownloadsCog.__new__(DevJarDownloadsCog)
            cog.settings = SimpleNamespace(
                dev_jar_download_upload_dir=str(upload_dir),
                release_webhook_secret="secret",
                dev_jar_download_public_base_url="https://downloads.example.test",
                dev_jar_download_download_path="/dev-download",
                discord_oauth_client_secret="client-secret",
                dev_jar_download_token_ttl_seconds=300,
            )
            cog.token_store = OneTimeDownloadTokenStore(now=lambda: 1999)
            response = FakeResponse()
            interaction = SimpleNamespace(
                user=SimpleNamespace(
                    id=123,
                    guild_permissions=SimpleNamespace(administrator=True),
                    roles=[],
                ),
                guild_id=456,
                response=response,
            )

            with patch("bulmaai.cogs.dev_jar_downloads.time.time", return_value=1999):
                await cog._handle_download_button(interaction, artifact.file_name)

        self.assertEqual(len(response.messages), 1)
        content, kwargs = response.messages[0]
        self.assertIn("Authorize with Discord", content)
        self.assertIn("https://discord.com/oauth2/authorize?client_id=1336867824815312906", content)
        self.assertIn(
            "redirect_uri=https%3A%2F%2Fdownloads.dragonminez.com%2Fdiscord%2Foauth%2Fcallback",
            content,
        )
        self.assertNotIn("One-time download link", content)
        self.assertTrue(kwargs["ephemeral"])

    async def test_cog_upload_payload_posts_download_announcement(self) -> None:
        class FakeChannel:
            def __init__(self) -> None:
                self.sent: list[dict] = []

            async def send(self, **kwargs) -> None:
                self.sent.append(kwargs)

        class FakeBot:
            def __init__(self, channels: dict[int, FakeChannel]) -> None:
                self._channels = channels

            def get_channel(self, channel_id: int) -> FakeChannel:
                return self._channels[channel_id]

        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            artifact = parse_dev_jar_filename("dragonminez-2.1.2__v2.1__222222222222.jar")
            (upload_dir / artifact.file_name).write_bytes(b"jar")
            patreon_channel = FakeChannel()
            testing_channel = FakeChannel()
            cog = DevJarDownloadsCog.__new__(DevJarDownloadsCog)
            cog.bot = FakeBot(
                {
                    1287883800805642351: patreon_channel,
                    1453303311330709674: testing_channel,
                }
            )
            cog.settings = SimpleNamespace(
                dev_jar_download_upload_dir=str(upload_dir),
            )

            await cog._handle_upload_payload(
                DevJarUploadPayload(
                    artifact=artifact,
                    sha256="a" * 64,
                    workflow_run_url="https://github.com/DragonMineZ/dragonminez/actions/runs/123",
                    commits=(
                        DevJarCommit(
                            sha="222222222222",
                            title="fix: race selection screen fix",
                            description=None,
                            author="Shokkoh",
                            url="https://github.com/DragonMineZ/dragonminez/commit/222222222222",
                        ),
                    ),
                )
            )

        self.assertEqual(len(patreon_channel.sent), 1)
        self.assertEqual(len(testing_channel.sent), 1)
        embed = patreon_channel.sent[0]["embed"]
        self.assertEqual(embed.url, "https://github.com/DragonMineZ/dragonminez/actions/runs/123")
        field_values = {field.name: field.value for field in embed.fields}
        self.assertEqual(field_values["Artifact"], f"`{artifact.file_name}`")
        self.assertEqual(field_values["Size"], "0.0 MB")
        self.assertEqual(field_values["SHA-256"], f"`{'a' * 64}`")

    async def test_discord_oauth_client_fetches_identity_guilds_and_member_roles(self) -> None:
        class FakeResponse:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        calls = AsyncMock(
            side_effect=[
                FakeResponse({"access_token": "discord-access-token"}),
                FakeResponse({"id": "123"}),
                FakeResponse([{"id": "456", "permissions": str(ADMINISTRATOR_PERMISSION)}]),
                FakeResponse({"roles": ["1286814599215317034"]}),
            ]
        )
        client = DiscordOAuthClient(
            client_id="client-id",
            client_secret="client-secret",
            redirect_uri="https://downloads.example.test/dev-download/oauth/callback",
        )

        with patch("bulmaai.services.dev_jar_downloads.request", calls):
            payload = await client.fetch_member_for_code("oauth-code", guild_id=456)

        self.assertEqual(
            payload,
            DiscordOAuthMember(
                user_id=123,
                guild_id=456,
                permissions=ADMINISTRATOR_PERMISSION,
                role_ids=(1286814599215317034,),
            ),
        )
        token_call = calls.await_args_list[0].kwargs
        user_call = calls.await_args_list[1].kwargs
        guilds_call = calls.await_args_list[2].kwargs
        member_call = calls.await_args_list[3].kwargs
        self.assertEqual(token_call["data"]["grant_type"], "authorization_code")
        self.assertEqual(token_call["data"]["code"], "oauth-code")
        self.assertEqual(user_call["headers"]["Authorization"], "Bearer discord-access-token")
        self.assertEqual(guilds_call["headers"]["Authorization"], "Bearer discord-access-token")
        self.assertEqual(member_call["headers"]["Authorization"], "Bearer discord-access-token")

if __name__ == "__main__":
    unittest.main()
