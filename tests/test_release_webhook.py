import asyncio
import http.client
import json
import threading
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from bulmaai.services.release_webhook import (
    ReleaseWebhookHttpResponse,
    ReleaseWebhookServer,
    clear_extra_webhook_routes,
    handle_release_webhook_post,
    register_extra_get_route,
    register_extra_raw_webhook_route,
    register_extra_webhook_route,
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


class ReleaseWebhookTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_extra_webhook_routes()

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
            headers={"X-DMZ-Release-Bot-Secret": "secret"},
            expected_path="/dmz-release",
            secret="secret",
            submit_payload=lambda payload: None,
        )

        self.assertEqual(response.status, 404)

    def test_registered_extra_route_handles_dev_jar_path_before_release_parser(self) -> None:
        @dataclass(frozen=True)
        class ParsedPayload:
            remote_name: str

        queued = []

        def parse_dev_payload(payload: dict) -> ParsedPayload:
            return ParsedPayload(remote_name=str(payload["remote_name"]))

        register_extra_webhook_route(
            path="/dmz-dev-jar",
            secret="dev-secret",
            secret_header="X-DMZ-Release-Bot-Secret",
            parse_payload=parse_dev_payload,
            submit_payload=queued.append,
            accepted_body="Dev jar upload queued",
        )

        response = handle_release_webhook_post(
            path="/dmz-dev-jar",
            body=json.dumps({"remote_name": "dragonminez-2.1.2__222222222222.jar"}).encode("utf-8"),
            headers={"X-DMZ-Release-Bot-Secret": "dev-secret"},
            expected_path="/dmz-release",
            secret="release-secret",
            submit_payload=lambda payload: None,
        )

        self.assertEqual(response.status, 202)
        self.assertEqual(queued, [ParsedPayload("dragonminez-2.1.2__222222222222.jar")])

    def test_registered_extra_route_rejects_wrong_secret_before_release_parser(self) -> None:
        register_extra_webhook_route(
            path="/dmz-dev-jar",
            secret="dev-secret",
            secret_header="X-DMZ-Release-Bot-Secret",
            parse_payload=lambda payload: payload,
            submit_payload=lambda payload: None,
            accepted_body="Dev jar upload queued",
        )

        response = handle_release_webhook_post(
            path="/dmz-dev-jar",
            body=b"{invalid-json",
            headers={"X-DMZ-Release-Bot-Secret": "wrong"},
            expected_path="/dmz-release",
            secret="release-secret",
            submit_payload=lambda payload: None,
        )

        self.assertEqual(response.status, 403)

    def test_registered_raw_route_receives_body_and_headers(self) -> None:
        queued = []

        def handle_raw(body: bytes, headers):
            if headers.get("X-Test-Signature") != "signed":
                return ReleaseWebhookHttpResponse(status=403, body=b"Forbidden")
            queued.append(body)
            return ReleaseWebhookHttpResponse(status=202, body=b"Raw webhook queued")

        register_extra_raw_webhook_route(
            path="/patreon/webhook",
            handle_request=handle_raw,
        )

        response = handle_release_webhook_post(
            path="/patreon/webhook",
            body=b'{"data":{"id":"member-1"}}',
            headers={"X-Test-Signature": "signed"},
            expected_path="/dmz-release",
            secret="release-secret",
            submit_payload=lambda payload: None,
        )

        self.assertEqual(response.status, 202)
        self.assertEqual(response.body, "Raw webhook queued")
        self.assertEqual(queued, [b'{"data":{"id":"member-1"}}'])

    def test_registered_get_route_streams_file_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.jar"
            path.write_bytes(b"jar-bytes")
            completed = threading.Event()

            register_extra_get_route(
                path_prefix="/dev-download/",
                handle_request=lambda path, query: ReleaseWebhookHttpResponse(
                    status=200,
                    body=b"",
                    content_type="application/java-archive",
                    file_path=Path(tmp) / "artifact.jar",
                    download_name="artifact.jar",
                    on_stream_complete=completed.set,
                ),
            )

            loop = asyncio.new_event_loop()

            async def on_payload(payload: dict) -> None:
                raise AssertionError("GET request should not submit a payload")

            server = ReleaseWebhookServer(
                host="127.0.0.1",
                port=0,
                path="/dmz-release",
                secret="secret",
                loop=loop,
                on_payload=on_payload,
            )
            connection: http.client.HTTPConnection | None = None
            try:
                server.start()
                assert server._server is not None
                host, port = server._server.server_address
                connection = http.client.HTTPConnection(host, port, timeout=5)
                connection.request("GET", "/dev-download/token")
                response = connection.getresponse()
                body = response.read()

                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Content-Type"), "application/java-archive")
                self.assertEqual(body, b"jar-bytes")
                self.assertTrue(completed.wait(timeout=1))
            finally:
                if connection is not None:
                    connection.close()
                server.stop()
                loop.close()

    def test_invalid_json_is_rejected(self) -> None:
        response = handle_release_webhook_post(
            path="/dmz-release",
            body=b"{invalid-json",
            headers={"X-DMZ-Release-Bot-Secret": "secret"},
            expected_path="/dmz-release",
            secret="secret",
            submit_payload=lambda payload: None,
        )

        self.assertEqual(response.status, 400)
        self.assertIn("JSON", response.body)

    def test_invalid_secret_is_rejected_before_parsing_body(self) -> None:
        queued: list[dict] = []

        response = handle_release_webhook_post(
            path="/dmz-release",
            body=b"{invalid-json",
            headers={"X-DMZ-Release-Bot-Secret": "wrong"},
            expected_path="/dmz-release",
            secret="secret",
            submit_payload=queued.append,
        )

        self.assertEqual(response.status, 403)
        self.assertEqual(queued, [])

    def test_missing_secret_is_rejected_before_checking_path(self) -> None:
        queued: list[dict] = []

        response = handle_release_webhook_post(
            path="/wrong",
            body=json.dumps(VALID_PAYLOAD).encode("utf-8"),
            headers={},
            expected_path="/dmz-release",
            secret="secret",
            submit_payload=queued.append,
        )

        self.assertEqual(response.status, 403)
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
            headers={"X-DMZ-Release-Bot-Secret": "secret"},
            expected_path="/dmz-release",
            secret="secret",
            submit_payload=queued.append,
        )

        self.assertEqual(response.status, 400)
        self.assertIn("commit_sha", response.body)
        self.assertEqual(queued, [])

    def test_server_requires_secret(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            with self.assertRaisesRegex(ValueError, "secret"):
                ReleaseWebhookServer(
                    host="127.0.0.1",
                    port=0,
                    path="/dmz-release",
                    secret=None,
                    loop=loop,
                    on_payload=lambda payload: None,
                )
        finally:
            loop.close()

    def test_get_without_secret_is_rejected(self) -> None:
        loop = asyncio.new_event_loop()

        async def on_payload(payload: dict) -> None:
            raise AssertionError("GET request should not submit a payload")

        server = ReleaseWebhookServer(
            host="127.0.0.1",
            port=0,
            path="/dmz-release",
            secret="secret",
            loop=loop,
            on_payload=on_payload,
        )
        connection: http.client.HTTPConnection | None = None
        try:
            server.start()
            assert server._server is not None
            host, port = server._server.server_address
            connection = http.client.HTTPConnection(host, port, timeout=5)
            connection.request("GET", "/.env")
            response = connection.getresponse()
            response.read()

            self.assertEqual(response.status, 403)
        finally:
            if connection is not None:
                connection.close()
            server.stop()
            loop.close()


if __name__ == "__main__":
    unittest.main()
