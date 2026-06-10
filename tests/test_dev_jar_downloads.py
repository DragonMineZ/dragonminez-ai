import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bulmaai.cogs.dev_jar_downloads import (
    DevJarDownloadsCog,
    build_dev_jar_download_embed,
)
from bulmaai.services.dev_jar_downloads import (
    DevJarCommit,
    DevJarUploadPayload,
    OneTimeDownloadTokenStore,
    find_latest_dev_jar,
    parse_dev_jar_upload_payload,
    parse_dev_jar_filename,
)


class DevJarDownloadsTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_dev_jar_filename_keeps_hyphenated_version_intact(self) -> None:
        artifact = parse_dev_jar_filename(
            "dragonminez-2.1.2-alpha__39cd4f1c1234.jar"
        )

        self.assertEqual(artifact.version, "2.1.2-alpha")
        self.assertEqual(artifact.commit_sha, "39cd4f1c1234")

    def test_parse_dev_jar_filename_rejects_path_like_names(self) -> None:
        with self.assertRaises(ValueError):
            parse_dev_jar_filename("../dragonminez-2.1.2__39cd4f1c1234.jar")

    def test_find_latest_dev_jar_uses_file_modified_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            older = upload_dir / "dragonminez-2.1.1__111111111111.jar"
            newer = upload_dir / "dragonminez-2.1.2__222222222222.jar"
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
        artifact = parse_dev_jar_filename("dragonminez-2.1.2__222222222222.jar")

        token = store.issue(artifact=artifact, requester_id=123, ttl_seconds=60)

        first = store.consume(token)
        second = store.consume(token)

        self.assertEqual(first, artifact)
        self.assertIsNone(second)

    def test_one_time_download_token_claim_completes_or_releases(self) -> None:
        store = OneTimeDownloadTokenStore(now=lambda: 1000)
        artifact = parse_dev_jar_filename("dragonminez-2.1.2__222222222222.jar")
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
        artifact = parse_dev_jar_filename("dragonminez-2.1.2__222222222222.jar")
        token = store.issue(artifact=artifact, requester_id=123, ttl_seconds=5)
        now = 1006.0

        self.assertIsNone(store.consume(token))

    def test_parse_dev_jar_upload_payload_requires_commit_metadata(self) -> None:
        with self.assertRaisesRegex(ValueError, "commits"):
            parse_dev_jar_upload_payload(
                {
                    "remote_name": "dragonminez-2.1.2__222222222222.jar",
                    "sha256": "a" * 64,
                }
            )

    def test_parse_dev_jar_upload_payload_accepts_required_commit_fields(self) -> None:
        payload = parse_dev_jar_upload_payload(
            {
                "remote_name": "dragonminez-2.1.2__222222222222.jar",
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
        artifact = parse_dev_jar_filename("dragonminez-2.1.2__222222222222.jar")

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

        self.assertEqual(embed.title, "DragonMineZ Dev Update")
        self.assertEqual(embed.url, "https://github.com/DragonMineZ/dragonminez/actions/runs/123")
        field_values = [field.value for field in embed.fields]
        self.assertIn("`222222222222`", field_values)

    def test_download_embed_includes_commit_summary_links_titles_descriptions_and_authors(self) -> None:
        artifact = parse_dev_jar_filename("dragonminez-2.1.2__086afb963f2c.jar")

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
        self.assertIn("[9306605](https://github.com/DragonMineZ/dragonminez/commit/93066058a79b)", field_values["Commits Changelog"])
        self.assertIn("feat: changed form drains", field_values["Commits Changelog"])
        self.assertIn("Adds support for new drain behavior.", field_values["Commits Changelog"])
        self.assertIn("- Shokkoh", field_values["Commits Changelog"])
        self.assertIn("[086afb9](https://github.com/DragonMineZ/dragonminez/commit/086afb963f2c)", field_values["Commits Changelog"])
        self.assertIn("fix: race selection screen fix", field_values["Commits Changelog"])

    def test_cog_direct_token_download_consumes_token_after_successful_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            artifact = parse_dev_jar_filename("dragonminez-2.1.2__222222222222.jar")
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
            artifact = parse_dev_jar_filename("dragonminez-2.1.2__222222222222.jar")
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

    async def test_download_button_sends_direct_link_for_authorized_member(self) -> None:
        class FakeResponse:
            def __init__(self) -> None:
                self.messages: list[tuple[str, dict]] = []

            async def send_message(self, content: str, **kwargs) -> None:
                self.messages.append((content, kwargs))

        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            artifact = parse_dev_jar_filename("dragonminez-2.1.2__222222222222.jar")
            (upload_dir / artifact.file_name).write_bytes(b"jar")
            cog = DevJarDownloadsCog.__new__(DevJarDownloadsCog)
            cog.settings = SimpleNamespace(
                dev_jar_download_upload_dir=str(upload_dir),
                release_webhook_secret="secret",
                dev_jar_download_public_base_url="https://downloads.example.test",
                dev_jar_download_download_path="/dev-download",
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
        self.assertIn("One-time download link", content)
        self.assertIn("https://downloads.example.test/dev-download/", content)
        self.assertNotIn("discord.com/oauth2/authorize", content)
        self.assertTrue(kwargs["ephemeral"])

    async def test_download_button_rejects_unauthorized_member_without_oauth(self) -> None:
        class FakeResponse:
            def __init__(self) -> None:
                self.messages: list[tuple[str, dict]] = []

            async def send_message(self, content: str, **kwargs) -> None:
                self.messages.append((content, kwargs))

        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            artifact = parse_dev_jar_filename("dragonminez-2.1.2__222222222222.jar")
            (upload_dir / artifact.file_name).write_bytes(b"jar")
            cog = DevJarDownloadsCog.__new__(DevJarDownloadsCog)
            cog.settings = SimpleNamespace(
                dev_jar_download_upload_dir=str(upload_dir),
                release_webhook_secret="secret",
                dev_jar_download_public_base_url="https://downloads.example.test",
                dev_jar_download_download_path="/dev-download",
                dev_jar_download_token_ttl_seconds=300,
            )
            cog.token_store = OneTimeDownloadTokenStore(now=lambda: 1999)
            response = FakeResponse()
            interaction = SimpleNamespace(
                user=SimpleNamespace(
                    id=123,
                    guild_permissions=SimpleNamespace(administrator=False),
                    roles=[SimpleNamespace(id=999)],
                ),
                guild_id=456,
                response=response,
            )

            await cog._handle_download_button(interaction, artifact.file_name)

        self.assertEqual(len(response.messages), 1)
        content, kwargs = response.messages[0]
        self.assertIn("not authorized", content)
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
            artifact = parse_dev_jar_filename("dragonminez-2.1.2__222222222222.jar")
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
        self.assertEqual(field_values["Size"], "0.000 MB")
        self.assertEqual(field_values["SHA-256"], f"`{'a' * 64}`")

if __name__ == "__main__":
    unittest.main()
