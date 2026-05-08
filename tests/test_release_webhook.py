import json
import unittest

from bulmaai.services.release_webhook import handle_release_webhook_post


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


class ReleaseWebhookTests(unittest.TestCase):
    def test_valid_post_queues_candidate_payload(self) -> None:
        queued: list[dict] = []

        response = handle_release_webhook_post(
            path="/dmz-release",
            body=json.dumps(VALID_PAYLOAD).encode("utf-8"),
            headers={"X-DMZ-Release-Bot-Secret": "secret"},
            expected_path="/dmz-release",
            secret="secret",
            submit_payload=queued.append,
        )

        self.assertEqual(response.status, 202)
        self.assertEqual(queued, [VALID_PAYLOAD])

    def test_invalid_path_is_rejected(self) -> None:
        response = handle_release_webhook_post(
            path="/wrong",
            body=json.dumps(VALID_PAYLOAD).encode("utf-8"),
            headers={},
            expected_path="/dmz-release",
            secret=None,
            submit_payload=lambda payload: None,
        )

        self.assertEqual(response.status, 404)

    def test_invalid_json_is_rejected(self) -> None:
        response = handle_release_webhook_post(
            path="/dmz-release",
            body=b"{invalid-json",
            headers={},
            expected_path="/dmz-release",
            secret=None,
            submit_payload=lambda payload: None,
        )

        self.assertEqual(response.status, 400)
        self.assertIn("JSON", response.body)

    def test_invalid_secret_is_rejected(self) -> None:
        queued: list[dict] = []

        response = handle_release_webhook_post(
            path="/dmz-release",
            body=json.dumps(VALID_PAYLOAD).encode("utf-8"),
            headers={"X-DMZ-Release-Bot-Secret": "wrong"},
            expected_path="/dmz-release",
            secret="secret",
            submit_payload=queued.append,
        )

        self.assertEqual(response.status, 401)
        self.assertEqual(queued, [])

    def test_invalid_candidate_payload_is_rejected(self) -> None:
        invalid_payload = {
            **VALID_PAYLOAD,
            "client_payload": {**VALID_PAYLOAD["client_payload"], "commit_sha": ""},
        }
        queued: list[dict] = []

        response = handle_release_webhook_post(
            path="/dmz-release",
            body=json.dumps(invalid_payload).encode("utf-8"),
            headers={},
            expected_path="/dmz-release",
            secret=None,
            submit_payload=queued.append,
        )

        self.assertEqual(response.status, 400)
        self.assertIn("commit_sha", response.body)
        self.assertEqual(queued, [])


if __name__ == "__main__":
    unittest.main()
