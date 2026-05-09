import os
import unittest
from unittest.mock import AsyncMock, patch


os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.config import get_editable_setting_names, load_settings
from bulmaai.cogs.release_approval import ReleaseApprovalCog
from bulmaai.github.github_app_auth import GitHubAppAuth
from bulmaai.github.github_service import GitHubService
from bulmaai.services.release_approval import (
    APPROVED_EVENT_TYPE,
    ReleaseCandidate,
    ReleaseCandidateError,
    ReleasePublishMetadataError,
    ReleaseApprovalService,
    build_approval_dispatch_payload,
    parse_release_candidate_payload,
    validate_publish_metadata,
)
from bulmaai.ui.release_views import (
    build_release_candidate_embed,
    can_manage_release_approval,
)


VALID_PAYLOAD = {
    "event_type": "dragonminez_release_candidate",
    "client_payload": {
        "version": "2.1.2",
        "release_type": "release",
        "minecraft_version": "1.20.1",
        "forge_version": "47.4.10",
        "commit_sha": "approved-main-commit",
        "artifact_name": "dragonminez-2.1.2.jar",
        "artifact_sha256": "sha256-from-prepare-build",
        "targets": ["modrinth", "curseforge"],
        "workflow_run_url": "https://github.com/DragonMineZ/dragonminez/actions/runs/123",
    },
}


class ReleaseApprovalTests(unittest.TestCase):
    def test_release_settings_defaults_match_dmz_release_channel(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DISCORD_TOKEN": "dummy-discord-token",
                "OPENAI_KEY": "dummy-openai-key",
                "GH_APP_PRIVATE_KEY_PEM": "dummy-github-key",
            },
            clear=True,
        ):
            settings = load_settings(include_overrides=False)

        self.assertEqual(settings.releases_channel_id, 1216430625431748771)
        self.assertTrue(settings.release_webhook_enabled)
        self.assertEqual(settings.release_webhook_host, "0.0.0.0")
        self.assertEqual(settings.release_webhook_port, 8088)
        self.assertEqual(settings.release_webhook_path, "/dmz-release")
        self.assertIsNone(settings.release_webhook_secret)
        self.assertIn("bulmaai.cogs.release_approval", settings.initial_extensions)
        self.assertNotIn("release_webhook_secret", get_editable_setting_names())

    def test_parse_release_candidate_payload_preserves_required_fields(self) -> None:
        candidate = parse_release_candidate_payload(VALID_PAYLOAD)

        self.assertEqual(candidate.version, "2.1.2")
        self.assertEqual(candidate.commit_sha, "approved-main-commit")
        self.assertEqual(candidate.artifact_sha256, "sha256-from-prepare-build")
        self.assertEqual(candidate.targets, ("modrinth", "curseforge"))
        self.assertEqual(
            candidate.workflow_run_url,
            "https://github.com/DragonMineZ/dragonminez/actions/runs/123",
        )

    def test_parse_release_candidate_payload_rejects_wrong_event_type(self) -> None:
        payload = {**VALID_PAYLOAD, "event_type": "wrong"}

        with self.assertRaisesRegex(ReleaseCandidateError, "event_type"):
            parse_release_candidate_payload(payload)

    def test_parse_release_candidate_payload_requires_artifact_sha256(self) -> None:
        payload = {
            **VALID_PAYLOAD,
            "client_payload": {
                **VALID_PAYLOAD["client_payload"],
                "artifact_sha256": "",
            },
        }

        with self.assertRaisesRegex(ReleaseCandidateError, "artifact_sha256"):
            parse_release_candidate_payload(payload)

    def test_build_approval_dispatch_payload_includes_required_publish_args(self) -> None:
        candidate = ReleaseCandidate(
            version="2.1.2",
            release_type="release",
            minecraft_version="1.20.1",
            forge_version="47.4.10",
            commit_sha="abc123",
            artifact_name="dragonminez-2.1.2.jar",
            artifact_sha256="sha256",
            targets=("modrinth", "curseforge"),
            workflow_run_url=None,
        )

        payload = build_approval_dispatch_payload(
            candidate,
            approved_by="Bruno#0001",
            changelog="Release notes",
            update_description="Short update text",
        )

        self.assertEqual(payload["event_type"], APPROVED_EVENT_TYPE)
        self.assertEqual(
            payload["client_payload"],
            {
                "version": "2.1.2",
                "commit_sha": "abc123",
                "artifact_sha256": "sha256",
                "approved_by": "Bruno#0001",
                "changelog": "Release notes",
                "update_description": "Short update text",
            },
        )

    def test_publish_metadata_requires_changelog_and_update_description(self) -> None:
        candidate = parse_release_candidate_payload(VALID_PAYLOAD)

        with self.assertRaisesRegex(ReleasePublishMetadataError, "changelog"):
            validate_publish_metadata(candidate)

        with self.assertRaisesRegex(ReleasePublishMetadataError, "update_description"):
            validate_publish_metadata(
                ReleaseCandidate(
                    **{
                        **candidate.__dict__,
                        "changelog": "Release notes",
                        "update_description": " ",
                    }
                )
            )

    def test_build_approval_dispatch_payload_rejects_blank_publish_args(self) -> None:
        candidate = parse_release_candidate_payload(VALID_PAYLOAD)

        with self.assertRaises(ReleasePublishMetadataError):
            build_approval_dispatch_payload(
                candidate,
                approved_by="348174141121101824",
                changelog=" ",
                update_description="",
            )

    def test_release_controls_are_admin_only(self) -> None:
        admin = type(
            "Member",
            (),
            {"guild_permissions": type("Perms", (), {"administrator": True})()},
        )()
        staff_non_admin = type(
            "Member",
            (),
            {
                "guild_permissions": type("Perms", (), {"administrator": False})(),
                "roles": [type("Role", (), {"id": 1352882775304175668})()],
            },
        )()

        self.assertTrue(can_manage_release_approval(admin))
        self.assertFalse(can_manage_release_approval(staff_non_admin))

    def test_release_candidate_embed_exposes_safety_fields(self) -> None:
        candidate = parse_release_candidate_payload(VALID_PAYLOAD)

        embed = build_release_candidate_embed(candidate)
        field_values = {field.name: field.value for field in embed.fields}

        self.assertIn("DragonMineZ 2.1.2", embed.title)
        self.assertEqual(field_values["Commit"], "`approved-main-commit`")
        self.assertEqual(field_values["Artifact SHA-256"], "`sha256-from-prepare-build`")
        self.assertIn("modrinth", field_values["Targets"])
        self.assertEqual(embed.url, "https://github.com/DragonMineZ/dragonminez/actions/runs/123")

    def test_release_webhook_does_not_start_without_secret(self) -> None:
        settings = type(
            "Settings",
            (),
            {
                "release_webhook_enabled": True,
                "release_webhook_secret": None,
                "release_webhook_host": "127.0.0.1",
                "release_webhook_port": 0,
                "release_webhook_path": "/dmz-release",
            },
        )()
        cog = object.__new__(ReleaseApprovalCog)
        cog.settings = settings
        cog.webhook_server = None

        with self.assertLogs("bulmaai.cogs.release_approval", level="ERROR") as logs:
            cog._start_webhook_server()

        self.assertIsNone(cog.webhook_server)
        self.assertIn("DMZ_RELEASE_BOT_WEBHOOK_SECRET", "\n".join(logs.output))


class GitHubDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_release_approval_service_dispatches_approved_candidate(self) -> None:
        candidate = ReleaseCandidate(
            **{
                **parse_release_candidate_payload(VALID_PAYLOAD).__dict__,
                "changelog": "Release notes",
                "update_description": "Update text",
            }
        )
        github_service = type(
            "GitHubService",
            (),
            {"dispatch_repository_event": AsyncMock()},
        )()
        service = ReleaseApprovalService(github_service=github_service)

        await service.approve_candidate(
            candidate,
            approved_by="AdminUser",
        )

        github_service.dispatch_repository_event.assert_awaited_once_with(
            event_type=APPROVED_EVENT_TYPE,
            client_payload={
                "version": "2.1.2",
                "commit_sha": "approved-main-commit",
                "artifact_sha256": "sha256-from-prepare-build",
                "approved_by": "AdminUser",
                "changelog": "Release notes",
                "update_description": "Update text",
            },
        )

    async def test_dispatch_repository_event_posts_expected_github_request(self) -> None:
        auth = GitHubAppAuth(
            app_id="123",
            installation_id="456",
            private_key_pem="dummy-key",
        )
        auth.get_installation_token = AsyncMock(return_value="installation-token")
        service = GitHubService(auth=auth, owner="DragonMineZ", repo="dragonminez")

        with patch("bulmaai.github.github_service.request", new_callable=AsyncMock) as request_mock:
            response = request_mock.return_value
            response.raise_for_status = unittest.mock.Mock()

            await service.dispatch_repository_event(
                event_type=APPROVED_EVENT_TYPE,
                client_payload={"version": "2.1.2"},
            )

        request_mock.assert_awaited_once_with(
            "POST",
            "https://api.github.com/repos/DragonMineZ/dragonminez/dispatches",
            headers={
                "Authorization": "Bearer installation-token",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={
                "event_type": APPROVED_EVENT_TYPE,
                "client_payload": {"version": "2.1.2"},
            },
        )
        response.raise_for_status.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
