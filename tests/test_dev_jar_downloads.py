import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from bulmaai.config import Settings
from bulmaai.cogs.dev_jar_downloads import (
    DevJarDownloadsCog,
    DevJarDownloadView,
)
from bulmaai.services.patch_notes import build_patch_notes_url
from bulmaai.services.dev_jar_downloads import (
    DevJarCommit,
    DevJarUploadPayload,
    OneTimeDownloadTokenStore,
    build_dev_jar_commit_layout,
    find_latest_dev_jar,
    format_dev_jar_commit_line,
    merge_dev_jar_commits,
    parse_dev_jar_upload_payload,
    parse_dev_jar_filename,
)
from bulmaai.ui.dev_jar_views import (
    build_dev_jar_download_embed,
    build_dev_jar_download_embeds,
    build_dev_jar_review_embeds,
)

MAIN_GUILD = SimpleNamespace(id=Settings.panel_guild_id)


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

    def test_merge_dev_jar_commits_deduplicates_by_sha_and_preserves_order(self) -> None:
        first = DevJarCommit(
            sha="111111111111", title="feat: one", description=None, author="A", url="https://x/1"
        )
        second = DevJarCommit(
            sha="222222222222", title="feat: two", description=None, author="A", url="https://x/2"
        )
        duplicate_of_first = DevJarCommit(
            sha="111111111111",
            title="feat: one (again)",
            description=None,
            author="A",
            url="https://x/1",
        )

        merged = merge_dev_jar_commits((first,), (duplicate_of_first, second))

        self.assertEqual(merged, (first, second))

    def test_format_dev_jar_commit_line_does_not_truncate_long_titles(self) -> None:
        long_title = "feat: " + ("a very long commit title " * 5).strip()
        commit = DevJarCommit(
            sha="abcdef123456",
            title=long_title,
            description=None,
            author="Shokkoh",
            url="https://github.com/DragonMineZ/dragonminez/commit/abcdef123456",
        )

        line = format_dev_jar_commit_line(commit)

        self.assertIn(long_title, line)

    def test_build_dev_jar_commit_layout_spills_into_continuation_descriptions(self) -> None:
        commits = tuple(
            DevJarCommit(
                sha=f"{i:012x}",
                title=f"fix: commit number {i} with a reasonably descriptive title",
                description=None,
                author="Shokkoh",
                url=f"https://github.com/DragonMineZ/dragonminez/commit/{i:012x}",
            )
            for i in range(40)
        )

        layout = build_dev_jar_commit_layout(commits, base_char_count=0, char_budget=10000)

        self.assertFalse(layout.overflowed)
        self.assertGreater(len(layout.descriptions), 1)
        combined = "\n".join(layout.descriptions)
        for commit in commits:
            self.assertIn(commit.title, combined)

    def test_build_dev_jar_commit_layout_falls_back_to_file_when_over_budget(self) -> None:
        commits = tuple(
            DevJarCommit(
                sha=f"{i:012x}",
                title=f"fix: commit number {i} " + ("padding " * 20),
                description=None,
                author="Shokkoh",
                url=f"https://github.com/DragonMineZ/dragonminez/commit/{i:012x}",
            )
            for i in range(60)
        )

        layout = build_dev_jar_commit_layout(commits, base_char_count=0, char_budget=2000)

        self.assertTrue(layout.overflowed)
        for commit in commits:
            self.assertIn(commit.title, layout.full_changelog_text)

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

    def test_download_embed_shows_release_notes_not_raw_changelog(self) -> None:
        artifact = parse_dev_jar_filename("dragonminez-2.1.2__086afb963f2c.jar")

        embeds, commit_list_text = build_dev_jar_download_embeds(
            artifact,
            release_notes="Big balance changes and a shiny new form!",
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

        # The public embed shows the short blurb, not a raw commit dump.
        field_values = {field.name: field.value for embed in embeds for field in embed.fields}
        self.assertEqual(field_values["What's New"], "Big balance changes and a shiny new form!")
        self.assertNotIn("Commits Changelog", [embed.title for embed in embeds])

        # The full commit list is still available in full, unconditionally, as
        # the text handed back for the always-attached commits file.
        assert commit_list_text is not None
        self.assertIn(
            "[9306605](https://github.com/DragonMineZ/dragonminez/commit/93066058a79b)",
            commit_list_text,
        )
        self.assertIn("feat: changed form drains", commit_list_text)
        self.assertIn("- Shokkoh", commit_list_text)
        self.assertIn(
            "[086afb9](https://github.com/DragonMineZ/dragonminez/commit/086afb963f2c)",
            commit_list_text,
        )
        self.assertIn("fix: race selection screen fix", commit_list_text)
        self.assertNotIn("Adds support for new drain behavior.", commit_list_text)

    async def test_download_view_includes_dated_patch_notes_link_button(self) -> None:
        artifact = parse_dev_jar_filename("dragonminez-2.1.2__222222222222.jar")
        patch_notes_url = build_patch_notes_url(
            "dragonminez", "v2.1.x", "PATCH_NOTES-v2.1.1.md"
        )

        view = DevJarDownloadView(artifact, patch_notes_url=patch_notes_url)

        labels = [getattr(child, "label", "") for child in view.children]
        urls = [getattr(child, "url", None) for child in view.children]
        self.assertIn("Get download link", labels)
        self.assertTrue(any(label.startswith("Patch Notes – ") for label in labels))
        self.assertIn(patch_notes_url, urls)

    def test_download_embed_notes_patch_notes_day(self) -> None:
        artifact = parse_dev_jar_filename("dragonminez-2.1.2__222222222222.jar")

        embeds, _ = build_dev_jar_download_embeds(artifact, commits=())

        field_values = {field.name: field.value for embed in embeds for field in embed.fields}
        self.assertIn("patch notes", field_values["Patch Notes"].lower())

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
                dev_jar_patreon_role_ids=(1287877272224665640, 1287877305259130900),
                dev_jar_tester_role_ids=(1286814599215317034,),
            )
            cog.token_store = OneTimeDownloadTokenStore(now=lambda: 1999)
            response = FakeResponse()
            interaction = SimpleNamespace(
                user=SimpleNamespace(
                    id=123,
                    guild=MAIN_GUILD, guild_permissions=SimpleNamespace(administrator=True),
                    roles=[],
                ),
                guild_id=456,
                response=response,
            )

            with (
                patch("bulmaai.cogs.dev_jar_downloads.time.time", return_value=1999),
                patch(
                    "bulmaai.cogs.dev_jar_downloads.has_completed_dev_jar_download",
                    new=AsyncMock(return_value=False),
                ),
            ):
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
                dev_jar_patreon_role_ids=(1287877272224665640, 1287877305259130900),
                dev_jar_tester_role_ids=(1286814599215317034,),
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

    async def test_download_button_refuses_user_who_already_downloaded_jar(self) -> None:
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
                dev_jar_patreon_role_ids=(1287877272224665640, 1287877305259130900),
                dev_jar_tester_role_ids=(1286814599215317034,),
            )
            cog.token_store = OneTimeDownloadTokenStore(now=lambda: 1999)
            response = FakeResponse()
            interaction = SimpleNamespace(
                user=SimpleNamespace(
                    id=123,
                    guild=MAIN_GUILD, guild_permissions=SimpleNamespace(administrator=True),
                    roles=[],
                ),
                guild_id=456,
                response=response,
            )

            with patch(
                "bulmaai.cogs.dev_jar_downloads.has_completed_dev_jar_download",
                new=AsyncMock(return_value=True),
            ):
                await cog._handle_download_button(interaction, artifact.file_name)

        self.assertEqual(len(response.messages), 1)
        content, kwargs = response.messages[0]
        self.assertIn("already downloaded", content)
        self.assertNotIn("https://downloads.example.test/dev-download/", content)
        self.assertTrue(kwargs["ephemeral"])

    async def test_completed_stream_records_one_time_download_for_requester(self) -> None:
        recorded: list[tuple[int, str]] = []

        async def fake_record(user_id: int, file_name: str) -> None:
            recorded.append((user_id, file_name))

        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            artifact = parse_dev_jar_filename("dragonminez-2.1.2__222222222222.jar")
            (upload_dir / artifact.file_name).write_bytes(b"jar")
            cog = DevJarDownloadsCog.__new__(DevJarDownloadsCog)
            cog.settings = SimpleNamespace(dev_jar_download_upload_dir=str(upload_dir))
            cog.bot = SimpleNamespace(loop=asyncio.get_running_loop())
            cog.token_store = OneTimeDownloadTokenStore(now=lambda: 1000)
            token = cog.token_store.issue(
                artifact=artifact,
                requester_id=123,
                ttl_seconds=60,
            )

            with patch(
                "bulmaai.cogs.dev_jar_downloads.record_completed_dev_jar_download",
                new=fake_record,
            ):
                file_response = cog._handle_direct_token_file(token)
                assert file_response.on_stream_complete is not None
                file_response.on_stream_complete()
                for _ in range(50):
                    if recorded:
                        break
                    await asyncio.sleep(0.01)

        self.assertEqual(file_response.status, 200)
        self.assertEqual(recorded, [(123, artifact.file_name)])
        self.assertEqual(cog._handle_direct_token_file(token).status, 403)

    async def test_cog_upload_payload_posts_to_review_channel_not_public(self) -> None:
        class FakeMessage:
            def __init__(self, message_id: int) -> None:
                self.id = message_id

        class FakeChannel:
            def __init__(self, channel_id: int) -> None:
                self.id = channel_id
                self.sent: list[dict] = []

            async def send(self, **kwargs) -> "FakeMessage":
                self.sent.append(kwargs)
                return FakeMessage(999)

        class FakeBot:
            def __init__(self, channels: dict[int, FakeChannel]) -> None:
                self._channels = channels

            def get_channel(self, channel_id: int):
                return self._channels.get(channel_id)

        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            artifact = parse_dev_jar_filename("dragonminez-2.1.2__222222222222.jar")
            (upload_dir / artifact.file_name).write_bytes(b"jar")

            review_channel = FakeChannel(1370061119586173070)
            patreon_channel = FakeChannel(1516564287210913932)
            testing_channel = FakeChannel(1453303311330709674)

            cog = DevJarDownloadsCog.__new__(DevJarDownloadsCog)
            cog.bot = FakeBot(
                {
                    1370061119586173070: review_channel,
                    1516564287210913932: patreon_channel,
                    1453303311330709674: testing_channel,
                }
            )
            cog.settings = SimpleNamespace(
                dev_jar_download_upload_dir=str(upload_dir),
                dev_jar_review_channel_id=1370061119586173070,
                discord_staff_role_ids=(1352882775304175668,),
            )
            cog._pending_review_lock = asyncio.Lock()

            with (
                patch(
                    "bulmaai.cogs.dev_jar_downloads.get_pending_dev_jar_review",
                    new=AsyncMock(return_value=None),
                ),
                patch(
                    "bulmaai.cogs.dev_jar_downloads.upsert_pending_dev_jar_review",
                    new=AsyncMock(),
                ),
                patch(
                    "bulmaai.cogs.dev_jar_downloads.set_pending_dev_jar_review_message",
                    new=AsyncMock(),
                ) as set_message_mock,
            ):
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

        self.assertEqual(len(review_channel.sent), 1)
        self.assertEqual(len(patreon_channel.sent), 0)
        self.assertEqual(len(testing_channel.sent), 0)
        embeds = review_channel.sent[0]["embeds"]
        self.assertEqual(embeds[0].title, "DragonMineZ Dev Jar Review")
        field_values = {field.name: field.value for field in embeds[0].fields}
        self.assertEqual(field_values["Status"], "Pending review")
        self.assertEqual(field_values["Commits since last decision"], "1")
        view = review_channel.sent[0]["view"]
        labels = [child.label for child in view.children]
        self.assertIn("Publish", labels)
        self.assertIn("Discard", labels)
        set_message_mock.assert_awaited_once_with(1370061119586173070, 999)

    async def test_cog_upload_payload_reposts_fresh_prompt_with_merged_commits(self) -> None:
        class FakeMessage:
            def __init__(self, message_id: int) -> None:
                self.id = message_id
                self.edits: list[dict] = []
                self.deleted = False

            async def edit(self, **kwargs) -> None:
                self.edits.append(kwargs)

            async def delete(self) -> None:
                self.deleted = True

        class FakeChannel:
            def __init__(self) -> None:
                self.id = 1370061119586173070
                self.sent: list[dict] = []
                self._messages: dict[int, "FakeMessage"] = {}
                self._next_id = 1

            async def send(self, **kwargs) -> "FakeMessage":
                self.sent.append(kwargs)
                message = FakeMessage(self._next_id)
                self._messages[message.id] = message
                self._next_id += 1
                return message

            async def fetch_message(self, message_id: int) -> "FakeMessage":
                return self._messages[message_id]

        class FakeBot:
            def __init__(self, channel: FakeChannel) -> None:
                self._channel = channel

            def get_channel(self, channel_id: int):
                return self._channel

        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            first_artifact = parse_dev_jar_filename("dragonminez-2.1.1__111111111111.jar")
            second_artifact = parse_dev_jar_filename("dragonminez-2.1.2__222222222222.jar")
            (upload_dir / first_artifact.file_name).write_bytes(b"jar")
            (upload_dir / second_artifact.file_name).write_bytes(b"jar")

            review_channel = FakeChannel()
            cog = DevJarDownloadsCog.__new__(DevJarDownloadsCog)
            cog.bot = FakeBot(review_channel)
            cog.settings = SimpleNamespace(
                dev_jar_download_upload_dir=str(upload_dir),
                dev_jar_review_channel_id=1370061119586173070,
                discord_staff_role_ids=(1352882775304175668,),
            )
            cog._pending_review_lock = asyncio.Lock()

            commit_one = DevJarCommit(
                sha="111111111111",
                title="feat: first commit",
                description=None,
                author="Shokkoh",
                url="https://github.com/DragonMineZ/dragonminez/commit/111111111111",
            )
            commit_two = DevJarCommit(
                sha="222222222222",
                title="fix: second commit",
                description=None,
                author="Shokkoh",
                url="https://github.com/DragonMineZ/dragonminez/commit/222222222222",
            )

            stored_review = {"value": None}

            async def fake_get_pending():
                return stored_review["value"]

            async def fake_upsert(**kwargs) -> None:
                stored_review["value"] = SimpleNamespace(**kwargs)

            async def fake_set_message(channel_id: int, message_id: int) -> None:
                stored_review["value"] = SimpleNamespace(
                    **{
                        **stored_review["value"].__dict__,
                        "channel_id": channel_id,
                        "message_id": message_id,
                    }
                )

            with (
                patch(
                    "bulmaai.cogs.dev_jar_downloads.get_pending_dev_jar_review",
                    new=fake_get_pending,
                ),
                patch(
                    "bulmaai.cogs.dev_jar_downloads.upsert_pending_dev_jar_review",
                    new=fake_upsert,
                ),
                patch(
                    "bulmaai.cogs.dev_jar_downloads.set_pending_dev_jar_review_message",
                    new=fake_set_message,
                ),
            ):
                await cog._handle_upload_payload(
                    DevJarUploadPayload(
                        artifact=first_artifact,
                        sha256="a" * 64,
                        workflow_run_url=None,
                        commits=(commit_one,),
                    )
                )
                await cog._handle_upload_payload(
                    DevJarUploadPayload(
                        artifact=second_artifact,
                        sha256="b" * 64,
                        workflow_run_url=None,
                        commits=(commit_two,),
                    )
                )

        # Repost-fresh: the second push deletes the first prompt and posts a new
        # one at the bottom, rather than editing the (possibly scrolled-away) one.
        self.assertEqual(len(review_channel.sent), 2)
        self.assertTrue(review_channel._messages[1].deleted)
        embeds = review_channel.sent[1]["embeds"]
        field_values = {field.name: field.value for field in embeds[0].fields}
        self.assertEqual(field_values["Commits since last decision"], "2")
        all_field_text = "\n".join(
            [field.value for embed in embeds for field in embed.fields]
            + [embed.description or "" for embed in embeds]
        )
        self.assertIn("feat: first commit", all_field_text)
        self.assertIn("fix: second commit", all_field_text)

    async def test_discarded_commits_reappear_merged_on_next_push(self) -> None:
        class FakeMessage:
            def __init__(self, message_id: int) -> None:
                self.id = message_id
                self.edits: list[dict] = []
                self.deleted = False

            async def edit(self, **kwargs) -> None:
                self.edits.append(kwargs)

            async def delete(self) -> None:
                self.deleted = True

        class FakeChannel:
            def __init__(self) -> None:
                self.id = 1370061119586173070
                self.sent: list[dict] = []
                self._messages: dict[int, "FakeMessage"] = {}
                self._next_id = 1

            async def send(self, **kwargs) -> "FakeMessage":
                self.sent.append(kwargs)
                message = FakeMessage(self._next_id)
                self._messages[message.id] = message
                self._next_id += 1
                return message

            async def fetch_message(self, message_id: int) -> "FakeMessage":
                return self._messages[message_id]

        class FakeBot:
            def __init__(self, channel: FakeChannel) -> None:
                self._channel = channel

            def get_channel(self, channel_id: int):
                return self._channel

        class FakeResponse:
            async def defer(self, **kwargs) -> None:
                return None

        class FakeFollowup:
            def __init__(self) -> None:
                self.messages: list[tuple[str, dict]] = []

            async def send(self, content: str, **kwargs) -> None:
                self.messages.append((content, kwargs))

        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            first_artifact = parse_dev_jar_filename("dragonminez-2.1.1__111111111111.jar")
            second_artifact = parse_dev_jar_filename("dragonminez-2.1.2__222222222222.jar")
            (upload_dir / first_artifact.file_name).write_bytes(b"jar")
            (upload_dir / second_artifact.file_name).write_bytes(b"jar")

            review_channel = FakeChannel()
            cog = DevJarDownloadsCog.__new__(DevJarDownloadsCog)
            cog.bot = FakeBot(review_channel)
            cog.settings = SimpleNamespace(
                dev_jar_download_upload_dir=str(upload_dir),
                dev_jar_review_channel_id=1370061119586173070,
                discord_staff_role_ids=(1352882775304175668,),
            )
            cog._pending_review_lock = asyncio.Lock()

            commit_one = DevJarCommit(
                sha="111111111111",
                title="feat: first commit",
                description=None,
                author="Shokkoh",
                url="https://github.com/DragonMineZ/dragonminez/commit/111111111111",
            )
            commit_two = DevJarCommit(
                sha="222222222222",
                title="fix: second commit",
                description=None,
                author="Shokkoh",
                url="https://github.com/DragonMineZ/dragonminez/commit/222222222222",
            )

            stored_review = {"value": None}

            async def fake_get_pending():
                return stored_review["value"]

            async def fake_upsert(**kwargs) -> None:
                stored_review["value"] = SimpleNamespace(**kwargs)

            async def fake_set_message(channel_id: int, message_id: int) -> None:
                stored_review["value"] = SimpleNamespace(
                    **{
                        **stored_review["value"].__dict__,
                        "channel_id": channel_id,
                        "message_id": message_id,
                    }
                )

            async def fake_clear_message() -> None:
                if stored_review["value"] is not None:
                    stored_review["value"] = SimpleNamespace(
                        **{
                            **stored_review["value"].__dict__,
                            "channel_id": None,
                            "message_id": None,
                        }
                    )

            with (
                patch(
                    "bulmaai.cogs.dev_jar_downloads.get_pending_dev_jar_review",
                    new=fake_get_pending,
                ),
                patch(
                    "bulmaai.cogs.dev_jar_downloads.upsert_pending_dev_jar_review",
                    new=fake_upsert,
                ),
                patch(
                    "bulmaai.cogs.dev_jar_downloads.set_pending_dev_jar_review_message",
                    new=fake_set_message,
                ),
                patch(
                    "bulmaai.cogs.dev_jar_downloads.clear_pending_dev_jar_review_message",
                    new=fake_clear_message,
                ),
                patch(
                    "bulmaai.cogs.dev_jar_downloads.clear_pending_dev_jar_review",
                    new=AsyncMock(),
                ) as clear_mock,
            ):
                await cog._handle_upload_payload(
                    DevJarUploadPayload(
                        artifact=first_artifact,
                        sha256="a" * 64,
                        workflow_run_url=None,
                        commits=(commit_one,),
                    )
                )

                first_message = review_channel._messages[1]
                interaction = SimpleNamespace(
                    response=FakeResponse(),
                    followup=FakeFollowup(),
                    message=first_message,
                    user="StaffUser#0001",
                )
                await cog._discard_pending_review(
                    interaction,
                    SimpleNamespace(
                        artifact=stored_review["value"].artifact,
                        commits=stored_review["value"].commits,
                        sha256=stored_review["value"].sha256,
                        workflow_run_url=stored_review["value"].workflow_run_url,
                    ),
                )

                await cog._handle_upload_payload(
                    DevJarUploadPayload(
                        artifact=second_artifact,
                        sha256="b" * 64,
                        workflow_run_url=None,
                        commits=(commit_two,),
                    )
                )

            # Discard deletes the rejected jar from disk...
            self.assertFalse((upload_dir / first_artifact.file_name).exists())

        # Discard never wipes the commit cache; only Publish does.
        clear_mock.assert_not_awaited()
        # Discard keeps its record message (edited to "Discarded"), and the next
        # push reposts a fresh prompt rather than resurrecting that record.
        self.assertEqual(len(review_channel.sent), 2)
        self.assertFalse(first_message.deleted)
        self.assertEqual(len(first_message.edits), 1)
        discard_status = {
            field.name: field.value for field in first_message.edits[0]["embeds"][0].fields
        }["Status"]
        self.assertEqual(discard_status, "Discarded")

        requeue_embeds = review_channel.sent[1]["embeds"]
        requeue_fields = {field.name: field.value for field in requeue_embeds[0].fields}
        self.assertEqual(requeue_fields["Status"], "Pending review")
        self.assertEqual(requeue_fields["Commits since last decision"], "2")
        all_field_text = "\n".join(
            [field.value for embed in requeue_embeds for field in embed.fields]
            + [embed.description or "" for embed in requeue_embeds]
        )
        self.assertIn("feat: first commit", all_field_text)
        self.assertIn("fix: second commit", all_field_text)

    async def test_collect_protected_artifact_names_includes_published_and_pending(self) -> None:
        cog = DevJarDownloadsCog.__new__(DevJarDownloadsCog)
        pending_artifact = parse_dev_jar_filename("dragonminez-2.1.2__222222222222.jar")
        pending = SimpleNamespace(artifact=pending_artifact)

        with (
            patch(
                "bulmaai.cogs.dev_jar_downloads.get_published_dev_jar_file_name",
                new=AsyncMock(return_value="dragonminez-2.1.1__111111111111.jar"),
            ),
            patch(
                "bulmaai.cogs.dev_jar_downloads.get_pending_dev_jar_review",
                new=AsyncMock(return_value=pending),
            ),
        ):
            names = await cog._collect_protected_artifact_names()

        self.assertEqual(
            names,
            sorted(["dragonminez-2.1.1__111111111111.jar", "dragonminez-2.1.2__222222222222.jar"]),
        )

    async def test_collect_protected_artifact_names_handles_no_published_or_pending(self) -> None:
        cog = DevJarDownloadsCog.__new__(DevJarDownloadsCog)

        with (
            patch(
                "bulmaai.cogs.dev_jar_downloads.get_published_dev_jar_file_name",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "bulmaai.cogs.dev_jar_downloads.get_pending_dev_jar_review",
                new=AsyncMock(return_value=None),
            ),
        ):
            names = await cog._collect_protected_artifact_names()

        self.assertEqual(names, [])

    async def test_publish_pending_review_posts_publicly_and_clears_state(self) -> None:
        class FakeResponse:
            async def defer(self, **kwargs) -> None:
                return None

        class FakeFollowup:
            def __init__(self) -> None:
                self.messages: list[tuple[str, dict]] = []

            async def send(self, content: str, **kwargs) -> None:
                self.messages.append((content, kwargs))

        class FakeMessage:
            def __init__(self) -> None:
                self.edits: list[dict] = []

            async def edit(self, **kwargs) -> None:
                self.edits.append(kwargs)

        class FakeChannel:
            def __init__(self) -> None:
                self.sent: list[dict] = []

            async def send(self, **kwargs) -> None:
                self.sent.append(kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            artifact = parse_dev_jar_filename("dragonminez-2.1.2__222222222222.jar")
            (upload_dir / artifact.file_name).write_bytes(b"jar")

            patreon_channel = FakeChannel()
            testing_channel = FakeChannel()

            cog = DevJarDownloadsCog.__new__(DevJarDownloadsCog)
            cog.bot = SimpleNamespace(
                get_channel=lambda channel_id: {
                    1516564287210913932: patreon_channel,
                    1453303311330709674: testing_channel,
                }.get(channel_id)
            )
            cog.settings = SimpleNamespace(
                dev_jar_download_upload_dir=str(upload_dir),
                dev_jar_announcement_channel_ids=(1516564287210913932, 1453303311330709674),
                patch_notes_repo="dragonminez",
                patch_notes_branch="v2.1.x",
                patch_notes_file_path="PATCH_NOTES-v2.1.1.md",
                openai_model="gpt-5-mini",
            )
            cog._openai_client = None
            cog._pending_review_lock = asyncio.Lock()

            review = SimpleNamespace(
                artifact=artifact,
                commits=(
                    DevJarCommit(
                        sha="222222222222",
                        title="fix: race selection screen fix",
                        description=None,
                        author="Shokkoh",
                        url="https://github.com/DragonMineZ/dragonminez/commit/222222222222",
                    ),
                ),
                sha256="a" * 64,
                workflow_run_url=None,
            )
            message = FakeMessage()
            interaction = SimpleNamespace(
                response=FakeResponse(),
                followup=FakeFollowup(),
                message=message,
                user="StaffUser#0001",
            )

            with (
                patch(
                    "bulmaai.cogs.dev_jar_downloads.get_pending_dev_jar_review",
                    new=AsyncMock(return_value=review),
                ),
                patch(
                    "bulmaai.cogs.dev_jar_downloads.clear_pending_dev_jar_review",
                    new=AsyncMock(),
                ) as clear_mock,
                patch(
                    "bulmaai.cogs.dev_jar_downloads.set_published_dev_jar_file_name",
                    new=AsyncMock(),
                ) as set_published_mock,
            ):
                await cog._publish_pending_review(interaction, review)

        self.assertEqual(len(patreon_channel.sent), 1)
        self.assertEqual(len(testing_channel.sent), 1)
        clear_mock.assert_awaited_once()
        set_published_mock.assert_awaited_once_with(artifact.file_name)
        self.assertEqual(len(message.edits), 1)
        self.assertIsNone(message.edits[0]["view"])
        self.assertTrue(
            any("published" in content.lower() for content, _ in interaction.followup.messages)
        )

    async def test_queue_for_review_refuses_to_publish_when_review_channel_unset(self) -> None:
        class FakeChannel:
            def __init__(self, channel_id: int) -> None:
                self.id = channel_id
                self.sent: list[dict] = []

            async def send(self, **kwargs) -> None:
                self.sent.append(kwargs)

        class FakeBot:
            def __init__(self, channels: dict[int, FakeChannel]) -> None:
                self._channels = channels

            def get_channel(self, channel_id: int):
                return self._channels.get(channel_id)

        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            artifact = parse_dev_jar_filename("dragonminez-2.1.2__222222222222.jar")
            (upload_dir / artifact.file_name).write_bytes(b"jar")

            announcement_channel = FakeChannel(1516564287210913932)

            cog = DevJarDownloadsCog.__new__(DevJarDownloadsCog)
            cog.bot = FakeBot({1516564287210913932: announcement_channel})
            cog.settings = SimpleNamespace(
                dev_jar_download_upload_dir=str(upload_dir),
                dev_jar_review_channel_id=None,
                dev_jar_announcement_channel_ids=(1516564287210913932,),
            )
            cog._pending_review_lock = asyncio.Lock()

            upsert_calls: list[dict] = []

            async def fake_upsert(**kwargs) -> None:
                upsert_calls.append(kwargs)

            with (
                patch(
                    "bulmaai.cogs.dev_jar_downloads.get_pending_dev_jar_review",
                    new=AsyncMock(return_value=None),
                ),
                patch(
                    "bulmaai.cogs.dev_jar_downloads.upsert_pending_dev_jar_review",
                    new=fake_upsert,
                ),
                self.assertLogs("bulmaai.cogs.dev_jar_downloads", level="ERROR") as log_capture,
            ):
                await cog._handle_upload_payload(
                    DevJarUploadPayload(
                        artifact=artifact,
                        sha256="a" * 64,
                        workflow_run_url=None,
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

        # With no staff review channel configured, this must refuse to publish
        # rather than silently bypass review and post to the public channel.
        self.assertEqual(announcement_channel.sent, [])
        # But the build and its commits are still persisted, protecting the
        # jar from VPS pruning until a review channel is configured.
        self.assertEqual(len(upsert_calls), 1)
        self.assertEqual(upsert_calls[0]["artifact"].file_name, artifact.file_name)
        self.assertEqual(len(upsert_calls[0]["commits"]), 1)
        self.assertTrue(any("refusing to publish" in message for message in log_capture.output))

    async def test_post_download_announcement_creates_feedback_thread_per_channel(self) -> None:
        class FakeMessage:
            def __init__(self) -> None:
                self.threads: list[str] = []

            async def create_thread(self, *, name: str) -> None:
                self.threads.append(name)

        class FakeChannel:
            def __init__(self) -> None:
                self.sent_messages: list[FakeMessage] = []

            async def send(self, **kwargs) -> "FakeMessage":
                message = FakeMessage()
                self.sent_messages.append(message)
                return message

        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            artifact = parse_dev_jar_filename("dragonminez-2.1.2__222222222222.jar")
            (upload_dir / artifact.file_name).write_bytes(b"jar")

            patreon_channel = FakeChannel()
            testing_channel = FakeChannel()

            cog = DevJarDownloadsCog.__new__(DevJarDownloadsCog)
            cog.bot = SimpleNamespace(
                get_channel=lambda channel_id: {
                    1516564287210913932: patreon_channel,
                    1453303311330709674: testing_channel,
                }.get(channel_id)
            )
            cog.settings = SimpleNamespace(
                dev_jar_download_upload_dir=str(upload_dir),
                dev_jar_announcement_channel_ids=(1516564287210913932, 1453303311330709674),
                patch_notes_repo="dragonminez",
                patch_notes_branch="v2.1.x",
                patch_notes_file_path="PATCH_NOTES-v2.1.1.md",
                openai_model="gpt-5-mini",
            )
            cog._openai_client = None

            with patch(
                "bulmaai.cogs.dev_jar_downloads.set_published_dev_jar_file_name",
                new=AsyncMock(),
            ):
                await cog._post_download_announcement(
                    artifact,
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

        # Both announcement channels get a feedback thread, since a player
        # might only have access to one of them.
        self.assertEqual(patreon_channel.sent_messages[0].threads, [f"Build {artifact.version}"])
        self.assertEqual(testing_channel.sent_messages[0].threads, [f"Build {artifact.version}"])

    async def test_post_download_announcement_thread_failure_does_not_raise(self) -> None:
        class FakeMessage:
            async def create_thread(self, *, name: str) -> None:
                raise discord.HTTPException(
                    SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions"
                )

        class FakeChannel:
            async def send(self, **kwargs) -> "FakeMessage":
                return FakeMessage()

        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            artifact = parse_dev_jar_filename("dragonminez-2.1.2__222222222222.jar")
            (upload_dir / artifact.file_name).write_bytes(b"jar")

            channel = FakeChannel()

            cog = DevJarDownloadsCog.__new__(DevJarDownloadsCog)
            cog.bot = SimpleNamespace(get_channel=lambda channel_id: channel)
            cog.settings = SimpleNamespace(
                dev_jar_download_upload_dir=str(upload_dir),
                dev_jar_announcement_channel_ids=(1516564287210913932,),
                patch_notes_repo="dragonminez",
                patch_notes_branch="v2.1.x",
                patch_notes_file_path="PATCH_NOTES-v2.1.1.md",
                openai_model="gpt-5-mini",
            )
            cog._openai_client = None

            published: list[str] = []

            async def fake_set_published(file_name: str) -> None:
                published.append(file_name)

            with patch(
                "bulmaai.cogs.dev_jar_downloads.set_published_dev_jar_file_name",
                new=fake_set_published,
            ):
                # Must not raise even though thread creation fails (missing
                # permission / thread limit): the announcement already went out.
                await cog._post_download_announcement(
                    artifact,
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

        self.assertEqual(published, [artifact.file_name])

    async def test_discard_pending_review_keeps_commits_queued(self) -> None:
        class FakeResponse:
            async def defer(self, **kwargs) -> None:
                return None

        class FakeFollowup:
            def __init__(self) -> None:
                self.messages: list[tuple[str, dict]] = []

            async def send(self, content: str, **kwargs) -> None:
                self.messages.append((content, kwargs))

        class FakeMessage:
            def __init__(self) -> None:
                self.edits: list[dict] = []

            async def edit(self, **kwargs) -> None:
                self.edits.append(kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            artifact = parse_dev_jar_filename("dragonminez-2.1.2__222222222222.jar")
            (upload_dir / artifact.file_name).write_bytes(b"jar")

            cog = DevJarDownloadsCog.__new__(DevJarDownloadsCog)
            cog._pending_review_lock = asyncio.Lock()
            cog.settings = SimpleNamespace(dev_jar_download_upload_dir=str(upload_dir))

            review = SimpleNamespace(
                artifact=artifact,
                commits=(
                    DevJarCommit(
                        sha="222222222222",
                        title="fix: race selection screen fix",
                        description=None,
                        author="Shokkoh",
                        url="https://github.com/DragonMineZ/dragonminez/commit/222222222222",
                    ),
                ),
                sha256=None,
                workflow_run_url=None,
            )
            message = FakeMessage()
            interaction = SimpleNamespace(
                response=FakeResponse(),
                followup=FakeFollowup(),
                message=message,
                user="StaffUser#0001",
            )

            with (
                patch(
                    "bulmaai.cogs.dev_jar_downloads.get_pending_dev_jar_review",
                    new=AsyncMock(return_value=review),
                ),
                patch(
                    "bulmaai.cogs.dev_jar_downloads.clear_pending_dev_jar_review",
                    new=AsyncMock(),
                ) as clear_mock,
                patch(
                    "bulmaai.cogs.dev_jar_downloads.clear_pending_dev_jar_review_message",
                    new=AsyncMock(),
                ) as clear_message_mock,
            ):
                await cog._discard_pending_review(interaction, review)

            # The rejected jar is deleted from disk...
            self.assertFalse((upload_dir / artifact.file_name).exists())

        # Discard must NOT clear the cache: accumulated commits stay queued so
        # they reappear (merged with anything new) on the next push's prompt. It
        # only clears the message link (so the next push posts fresh).
        clear_mock.assert_not_awaited()
        clear_message_mock.assert_awaited_once()
        self.assertEqual(len(message.edits), 1)
        self.assertIsNone(message.edits[0]["view"])
        field_values = {
            field.name: field.value for field in message.edits[0]["embeds"][0].fields
        }
        self.assertEqual(field_values["Status"], "Discarded")
        self.assertTrue(
            any("discarded" in content.lower() for content, _ in interaction.followup.messages)
        )
        self.assertTrue(
            any("remain queued" in content.lower() for content, _ in interaction.followup.messages)
        )

    async def test_review_buttons_dispatch_from_reloaded_state(self) -> None:
        class FakeResponse:
            def __init__(self) -> None:
                self.messages: list[tuple[str, dict]] = []

            async def send_message(self, content: str, **kwargs) -> None:
                self.messages.append((content, kwargs))

        cog = DevJarDownloadsCog.__new__(DevJarDownloadsCog)
        cog.settings = SimpleNamespace(discord_staff_role_ids=(1352882775304175668,))

        artifact = parse_dev_jar_filename("dragonminez-2.1.2__222222222222.jar")
        review = SimpleNamespace(artifact=artifact)

        # Non-staff clicks are rejected before any state is loaded.
        nonstaff = SimpleNamespace(
            response=FakeResponse(),
            user=SimpleNamespace(
                guild_permissions=SimpleNamespace(administrator=False), roles=[]
            ),
        )
        await cog._handle_review_decision(nonstaff, publish=True)
        self.assertEqual(len(nonstaff.response.messages), 1)
        self.assertIn("Only staff", nonstaff.response.messages[0][0])

        # A staff click reloads the pending review from the DB (not the view) and
        # routes to the publish handler — this is what makes the buttons survive
        # a restart that wiped the in-memory view.
        publish_mock = AsyncMock()
        cog._publish_pending_review = publish_mock
        staff = SimpleNamespace(
            response=FakeResponse(),
            user=SimpleNamespace(
                guild=MAIN_GUILD, guild_permissions=SimpleNamespace(administrator=True), roles=[]
            ),
        )
        with patch(
            "bulmaai.cogs.dev_jar_downloads.get_pending_dev_jar_review",
            new=AsyncMock(return_value=review),
        ):
            await cog._handle_review_decision(staff, publish=True)
        publish_mock.assert_awaited_once_with(staff, review)

        # A staff click with nothing pending (e.g. already handled) gets a notice.
        staff_stale = SimpleNamespace(
            response=FakeResponse(),
            user=SimpleNamespace(
                guild=MAIN_GUILD, guild_permissions=SimpleNamespace(administrator=True), roles=[]
            ),
        )
        with patch(
            "bulmaai.cogs.dev_jar_downloads.get_pending_dev_jar_review",
            new=AsyncMock(return_value=None),
        ):
            await cog._handle_review_decision(staff_stale, publish=False)
        self.assertTrue(
            any("no longer pending" in content.lower() for content, _ in staff_stale.response.messages)
        )

    async def test_discard_reports_when_jar_delete_fails(self) -> None:
        class FakeResponse:
            async def defer(self, **kwargs) -> None:
                return None

        class FakeFollowup:
            def __init__(self) -> None:
                self.messages: list[tuple[str, dict]] = []

            async def send(self, content: str, **kwargs) -> None:
                self.messages.append((content, kwargs))

        class FakeMessage:
            def __init__(self) -> None:
                self.edits: list[dict] = []

            async def edit(self, **kwargs) -> None:
                self.edits.append(kwargs)

        cog = DevJarDownloadsCog.__new__(DevJarDownloadsCog)
        cog._pending_review_lock = asyncio.Lock()
        # Simulate the bot user lacking permission to delete the jar file.
        cog._delete_artifact_file = lambda artifact: False

        artifact = parse_dev_jar_filename("dragonminez-2.1.2__222222222222.jar")
        review = SimpleNamespace(
            artifact=artifact,
            commits=(
                DevJarCommit(
                    sha="222222222222",
                    title="fix: race selection screen fix",
                    description=None,
                    author="Shokkoh",
                    url="https://github.com/DragonMineZ/dragonminez/commit/222222222222",
                ),
            ),
            sha256=None,
            workflow_run_url=None,
        )
        interaction = SimpleNamespace(
            response=FakeResponse(),
            followup=FakeFollowup(),
            message=FakeMessage(),
            user="StaffUser#0001",
        )

        with (
            patch(
                "bulmaai.cogs.dev_jar_downloads.get_pending_dev_jar_review",
                new=AsyncMock(return_value=review),
            ),
            patch(
                "bulmaai.cogs.dev_jar_downloads.clear_pending_dev_jar_review_message",
                new=AsyncMock(),
            ),
        ):
            await cog._discard_pending_review(interaction, review)

        # The build is still discarded (record edited, commits kept), but the
        # message must not falsely claim the file was deleted.
        self.assertTrue(
            any(
                "could not be deleted" in content.lower()
                for content, _ in interaction.followup.messages
            )
        )
        self.assertTrue(
            any("remain queued" in content.lower() for content, _ in interaction.followup.messages)
        )

    async def test_changelog_show_rejects_non_staff(self) -> None:
        class FakeContext:
            def __init__(self, author) -> None:
                self.author = author
                self.responses: list[tuple[str, dict]] = []

            async def respond(self, content: str, **kwargs) -> None:
                self.responses.append((content, kwargs))

        cog = DevJarDownloadsCog.__new__(DevJarDownloadsCog)
        cog.settings = SimpleNamespace(discord_staff_role_ids=(1352882775304175668,))
        ctx = FakeContext(
            SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False), roles=[])
        )

        await cog.changelog_show.callback(cog, ctx)

        self.assertEqual(len(ctx.responses), 1)
        content, kwargs = ctx.responses[0]
        self.assertIn("Only staff", content)
        self.assertTrue(kwargs["ephemeral"])

    async def test_changelog_show_reports_cached_commit_count_for_staff(self) -> None:
        class FakeContext:
            def __init__(self, author) -> None:
                self.author = author
                self.responses: list[tuple[str, dict]] = []

            async def respond(self, content: str, **kwargs) -> None:
                self.responses.append((content, kwargs))

        cog = DevJarDownloadsCog.__new__(DevJarDownloadsCog)
        cog.settings = SimpleNamespace(discord_staff_role_ids=(1352882775304175668,))
        ctx = FakeContext(
            SimpleNamespace(guild=MAIN_GUILD, guild_permissions=SimpleNamespace(administrator=True), roles=[])
        )
        review = SimpleNamespace(
            commits=(
                DevJarCommit(
                    sha="111111111111",
                    title="feat: first commit",
                    description=None,
                    author="Shokkoh",
                    url="https://github.com/DragonMineZ/dragonminez/commit/111111111111",
                ),
                DevJarCommit(
                    sha="222222222222",
                    title="fix: second commit",
                    description=None,
                    author="Shokkoh",
                    url="https://github.com/DragonMineZ/dragonminez/commit/222222222222",
                ),
            )
        )

        with patch(
            "bulmaai.cogs.dev_jar_downloads.get_pending_dev_jar_review",
            new=AsyncMock(return_value=review),
        ):
            await cog.changelog_show.callback(cog, ctx)

        self.assertEqual(len(ctx.responses), 1)
        content, kwargs = ctx.responses[0]
        self.assertIn("2 commit", content)
        self.assertIn("feat: first commit", content)
        self.assertTrue(kwargs["ephemeral"])

    async def test_changelog_reset_rejects_non_staff_and_does_not_clear(self) -> None:
        class FakeContext:
            def __init__(self, author) -> None:
                self.author = author
                self.responses: list[tuple[str, dict]] = []

            async def respond(self, content: str, **kwargs) -> None:
                self.responses.append((content, kwargs))

        cog = DevJarDownloadsCog.__new__(DevJarDownloadsCog)
        cog.settings = SimpleNamespace(discord_staff_role_ids=(1352882775304175668,))
        ctx = FakeContext(
            SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False), roles=[])
        )

        with patch(
            "bulmaai.cogs.dev_jar_downloads.reset_pending_dev_jar_commits",
            new=AsyncMock(),
        ) as reset_mock:
            await cog.changelog_reset.callback(cog, ctx)

        reset_mock.assert_not_awaited()
        content, kwargs = ctx.responses[0]
        self.assertIn("Only staff", content)
        self.assertTrue(kwargs["ephemeral"])

    async def test_changelog_reset_clears_commits_for_staff(self) -> None:
        class FakeContext:
            def __init__(self, author) -> None:
                self.author = author
                self.responses: list[tuple[str, dict]] = []

            async def respond(self, content: str, **kwargs) -> None:
                self.responses.append((content, kwargs))

        cog = DevJarDownloadsCog.__new__(DevJarDownloadsCog)
        cog.settings = SimpleNamespace(discord_staff_role_ids=(1352882775304175668,))
        ctx = FakeContext(
            SimpleNamespace(guild=MAIN_GUILD, guild_permissions=SimpleNamespace(administrator=True), roles=[])
        )

        with patch(
            "bulmaai.cogs.dev_jar_downloads.reset_pending_dev_jar_commits",
            new=AsyncMock(),
        ) as reset_mock:
            await cog.changelog_reset.callback(cog, ctx)

        reset_mock.assert_awaited_once()
        content, kwargs = ctx.responses[0]
        self.assertIn("cleared", content.lower())
        self.assertTrue(kwargs["ephemeral"])

if __name__ == "__main__":
    unittest.main()
