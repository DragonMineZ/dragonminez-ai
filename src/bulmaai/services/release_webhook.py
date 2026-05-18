import asyncio
import json
import logging
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from bulmaai.services.release_approval import (
    ReleaseCandidateError,
    parse_release_candidate_payload,
)


log = logging.getLogger(__name__)

WEBHOOK_SECRET_HEADER = "X-DMZ-Release-Bot-Secret"


@dataclass(frozen=True)
class ReleaseWebhookResponse:
    status: int
    body: str


@dataclass(frozen=True)
class ReleaseWebhookHttpResponse:
    status: int
    body: bytes
    content_type: str = "text/plain; charset=utf-8"
    file_path: Path | None = None
    download_name: str | None = None
    on_stream_complete: Callable[[], None] | None = None
    on_stream_error: Callable[[BaseException], None] | None = None


@dataclass(frozen=True)
class ExtraWebhookRoute:
    path: str
    secret: str
    secret_header: str
    parse_payload: Callable[[dict[str, Any]], Any]
    submit_payload: Callable[[Any], None]
    accepted_body: str


@dataclass(frozen=True)
class ExtraGetRoute:
    path_prefix: str
    handle_request: Callable[[str, dict[str, list[str]]], ReleaseWebhookHttpResponse]


@dataclass(frozen=True)
class ExtraRawWebhookRoute:
    path: str
    handle_request: Callable[[bytes, Any], ReleaseWebhookHttpResponse]


FORBIDDEN_RESPONSE = ReleaseWebhookResponse(status=403, body="Forbidden")

_extra_routes: dict[str, ExtraWebhookRoute] = {}
_extra_get_routes: list[ExtraGetRoute] = []
_extra_raw_routes: dict[str, ExtraRawWebhookRoute] = {}


def text_http_response(status: int, body: str) -> ReleaseWebhookHttpResponse:
    return ReleaseWebhookHttpResponse(status=status, body=body.encode("utf-8"))


def register_extra_webhook_route(
    *,
    path: str,
    secret: str | None,
    secret_header: str,
    parse_payload: Callable[[dict[str, Any]], Any],
    submit_payload: Callable[[Any], None],
    accepted_body: str,
) -> None:
    if not secret:
        return
    _extra_routes[path] = ExtraWebhookRoute(
        path=path,
        secret=secret,
        secret_header=secret_header,
        parse_payload=parse_payload,
        submit_payload=submit_payload,
        accepted_body=accepted_body,
    )


def unregister_extra_webhook_route(path: str) -> None:
    _extra_routes.pop(path, None)


def clear_extra_webhook_routes() -> None:
    _extra_routes.clear()
    _extra_get_routes.clear()
    _extra_raw_routes.clear()


def register_extra_raw_webhook_route(
    *,
    path: str,
    handle_request: Callable[[bytes, Any], ReleaseWebhookHttpResponse],
) -> None:
    _extra_raw_routes[path] = ExtraRawWebhookRoute(
        path=path,
        handle_request=handle_request,
    )


def unregister_extra_raw_webhook_route(path: str) -> None:
    _extra_raw_routes.pop(path, None)


def register_extra_get_route(
    *,
    path_prefix: str,
    handle_request: Callable[[str, dict[str, list[str]]], ReleaseWebhookHttpResponse],
) -> None:
    _extra_get_routes[:] = [
        route for route in _extra_get_routes if route.path_prefix != path_prefix
    ]
    _extra_get_routes.append(
        ExtraGetRoute(path_prefix=path_prefix, handle_request=handle_request)
    )


def unregister_extra_get_route(path_prefix: str) -> None:
    _extra_get_routes[:] = [
        route for route in _extra_get_routes if route.path_prefix != path_prefix
    ]


def handle_release_webhook_get(*, path: str, query: str = "") -> ReleaseWebhookHttpResponse:
    for route in sorted(_extra_get_routes, key=lambda item: len(item.path_prefix), reverse=True):
        if path.startswith(route.path_prefix):
            return route.handle_request(path, parse_qs(query))
    return text_http_response(403, "Forbidden")


def _get_header(headers: Any, name: str) -> str | None:
    if hasattr(headers, "get"):
        return headers.get(name)
    return None


def _has_valid_secret(headers: Any, secret: str | None) -> bool:
    if not secret:
        return False
    provided_secret = _get_header(headers, WEBHOOK_SECRET_HEADER)
    return bool(provided_secret) and compare_digest(provided_secret, secret)


def _has_valid_named_secret(headers: Any, *, secret: str | None, header_name: str) -> bool:
    if not secret:
        return False
    provided_secret = _get_header(headers, header_name)
    return bool(provided_secret) and compare_digest(provided_secret, secret)


def _parse_json_body(body: bytes) -> dict[str, Any] | ReleaseWebhookResponse:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ReleaseWebhookResponse(status=400, body="Invalid JSON body")

    if not isinstance(payload, dict):
        return ReleaseWebhookResponse(status=400, body="JSON body must be an object")
    return payload


def _handle_extra_webhook_route(
    *,
    route: ExtraWebhookRoute,
    body: bytes,
    headers: Any,
) -> ReleaseWebhookResponse:
    if not _has_valid_named_secret(
        headers,
        secret=route.secret,
        header_name=route.secret_header,
    ):
        return FORBIDDEN_RESPONSE

    payload = _parse_json_body(body)
    if isinstance(payload, ReleaseWebhookResponse):
        return payload

    try:
        parsed_payload = route.parse_payload(payload)
    except (KeyError, TypeError, ValueError) as error:
        return ReleaseWebhookResponse(status=400, body=str(error))

    route.submit_payload(parsed_payload)
    return ReleaseWebhookResponse(status=202, body=route.accepted_body)


def handle_release_webhook_post(
    *,
    path: str,
    body: bytes,
    headers: Any,
    expected_path: str,
    secret: str | None,
    submit_payload: Callable[[dict[str, Any]], None],
) -> ReleaseWebhookResponse:
    raw_route = _extra_raw_routes.get(path)
    if raw_route is not None:
        raw_response = raw_route.handle_request(body, headers)
        return ReleaseWebhookResponse(
            status=raw_response.status,
            body=raw_response.body.decode("utf-8", errors="replace"),
        )

    extra_route = _extra_routes.get(path)
    if extra_route is not None:
        return _handle_extra_webhook_route(
            route=extra_route,
            body=body,
            headers=headers,
        )

    if not _has_valid_secret(headers, secret):
        return FORBIDDEN_RESPONSE

    if path != expected_path:
        return ReleaseWebhookResponse(status=404, body="Not found")

    payload = _parse_json_body(body)
    if isinstance(payload, ReleaseWebhookResponse):
        return payload

    try:
        parse_release_candidate_payload(payload)
    except ReleaseCandidateError as error:
        return ReleaseWebhookResponse(status=400, body=str(error))

    submit_payload(payload)
    return ReleaseWebhookResponse(status=202, body="Release candidate queued")


class ReleaseWebhookServer:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        path: str,
        secret: str | None,
        loop: asyncio.AbstractEventLoop,
        on_payload: Callable[[dict[str, Any]], Awaitable[None]],
    ):
        if not secret:
            raise ValueError("Release webhook secret is required")
        self.host = host
        self.port = port
        self.path = path
        self.secret = secret
        self.loop = loop
        self.on_payload = on_payload
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._server is not None:
            return

        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _send_release_response(self, response: ReleaseWebhookResponse) -> None:
                self.send_response(response.status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(response.body.encode("utf-8"))

            def _send_http_response(self, response: ReleaseWebhookHttpResponse) -> None:
                if response.file_path is None:
                    try:
                        self.send_response(response.status)
                        self.send_header("Content-Type", response.content_type)
                        self.send_header("Content-Length", str(len(response.body)))
                        self.end_headers()
                        self.wfile.write(response.body)
                    except (BrokenPipeError, ConnectionResetError):
                        log.info("Client disconnected while sending webhook text response")
                    return

                try:
                    file_size = response.file_path.stat().st_size
                    file_handle = response.file_path.open("rb")
                except OSError as error:
                    log.exception("Failed to open webhook file response")
                    if response.on_stream_error is not None:
                        response.on_stream_error(error)
                    self._send_http_response(text_http_response(404, "Not found"))
                    return

                try:
                    with file_handle as handle:
                        self.send_response(response.status)
                        self.send_header("Content-Type", response.content_type)
                        self.send_header("Content-Length", str(file_size))
                        if response.download_name:
                            self.send_header(
                                "Content-Disposition",
                                f'attachment; filename="{response.download_name}"',
                            )
                        self.end_headers()
                        while chunk := handle.read(1024 * 1024):
                            self.wfile.write(chunk)
                    if response.on_stream_complete is not None:
                        response.on_stream_complete()
                except (BrokenPipeError, ConnectionResetError) as error:
                    log.info("Client disconnected while streaming webhook file response: %s", error)
                    if response.on_stream_error is not None:
                        response.on_stream_error(error)
                except OSError as error:
                    log.exception("Failed to stream webhook file response")
                    if response.on_stream_error is not None:
                        response.on_stream_error(error)

            def do_POST(self) -> None:
                content_length = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(content_length)
                response = handle_release_webhook_post(
                    path=self.path,
                    body=body,
                    headers=self.headers,
                    expected_path=owner.path,
                    secret=owner.secret,
                    submit_payload=owner._submit_payload,
                )
                self._send_release_response(response)

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                response = handle_release_webhook_get(
                    path=parsed.path,
                    query=parsed.query,
                )
                self._send_http_response(response)

            def _reject_unsupported_method(self) -> None:
                self._send_release_response(FORBIDDEN_RESPONSE)

            do_HEAD = _reject_unsupported_method
            do_PUT = _reject_unsupported_method
            do_PATCH = _reject_unsupported_method
            do_DELETE = _reject_unsupported_method
            do_OPTIONS = _reject_unsupported_method

            def log_message(self, format: str, *args: object) -> None:
                log.info("Release webhook: " + format, *args)

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="dmz-release-webhook",
            daemon=True,
        )
        self._thread.start()
        log.info("Release webhook server listening on %s:%s%s", self.host, self.port, self.path)

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _submit_payload(self, payload: dict[str, Any]) -> None:
        future = asyncio.run_coroutine_threadsafe(self.on_payload(payload), self.loop)

        def log_result(done_future: asyncio.Future[None]) -> None:
            try:
                done_future.result()
            except Exception:
                log.exception("Release webhook payload handling failed")

        future.add_done_callback(log_result)
