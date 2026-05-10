import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from bulmaai.services.dev_jar_downloads import DevJarUploadPayload
from bulmaai.services.dev_jar_webhook import (
    DevJarWebhookResponse,
    handle_dev_jar_webhook_post,
)


log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DevJarHttpResponse:
    status: int
    body: bytes
    content_type: str = "text/plain; charset=utf-8"
    file_path: Path | None = None
    download_name: str | None = None


def text_response(status: int, body: str) -> DevJarHttpResponse:
    return DevJarHttpResponse(status=status, body=body.encode("utf-8"))


class DevJarDownloadServer:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        webhook_path: str,
        download_path: str,
        oauth_callback_path: str,
        secret: str | None,
        loop: asyncio.AbstractEventLoop,
        on_payload: Callable[[DevJarUploadPayload], Awaitable[None]],
        on_direct_token: Callable[[str], DevJarHttpResponse],
        on_oauth_callback: Callable[[str, str], Awaitable[DevJarHttpResponse]],
    ):
        if not secret:
            raise ValueError("Dev jar webhook secret is required")
        self.host = host
        self.port = port
        self.webhook_path = webhook_path
        self.download_path = download_path.rstrip("/")
        self.oauth_callback_path = oauth_callback_path
        self.secret = secret
        self.loop = loop
        self.on_payload = on_payload
        self.on_direct_token = on_direct_token
        self.on_oauth_callback = on_oauth_callback
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._server is not None:
            return

        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _send_text(self, response: DevJarWebhookResponse | DevJarHttpResponse) -> None:
                body = response.body
                if isinstance(body, str):
                    body = body.encode("utf-8")
                self.send_response(response.status)
                self.send_header("Content-Type", getattr(response, "content_type", "text/plain; charset=utf-8"))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_http_response(self, response: DevJarHttpResponse) -> None:
                if response.file_path is None:
                    self._send_text(response)
                    return

                try:
                    file_size = response.file_path.stat().st_size
                    self.send_response(response.status)
                    self.send_header("Content-Type", response.content_type)
                    self.send_header("Content-Length", str(file_size))
                    if response.download_name:
                        self.send_header(
                            "Content-Disposition",
                            f'attachment; filename="{response.download_name}"',
                        )
                    self.end_headers()
                    with response.file_path.open("rb") as handle:
                        while chunk := handle.read(1024 * 1024):
                            self.wfile.write(chunk)
                except OSError:
                    log.exception("Failed to stream dev jar download")
                    self._send_text(text_response(404, "Not found"))

            def do_POST(self) -> None:
                content_length = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(content_length)
                response = handle_dev_jar_webhook_post(
                    path=urlparse(self.path).path,
                    body=body,
                    headers=self.headers,
                    expected_path=owner.webhook_path,
                    secret=owner.secret,
                    submit_payload=owner._submit_payload,
                )
                self._send_text(response)

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path.startswith(f"{owner.download_path}/"):
                    token = parsed.path.removeprefix(f"{owner.download_path}/")
                    self._send_http_response(owner.on_direct_token(token))
                    return

                if parsed.path == owner.oauth_callback_path:
                    query = parse_qs(parsed.query)
                    code = (query.get("code") or [""])[0]
                    state = (query.get("state") or [""])[0]
                    response = owner._run_oauth_callback(code, state)
                    self._send_http_response(response)
                    return

                self._send_text(text_response(404, "Not found"))

            def _reject_unsupported_method(self) -> None:
                self._send_text(text_response(403, "Forbidden"))

            do_HEAD = _reject_unsupported_method
            do_PUT = _reject_unsupported_method
            do_PATCH = _reject_unsupported_method
            do_DELETE = _reject_unsupported_method
            do_OPTIONS = _reject_unsupported_method

            def log_message(self, format: str, *args: object) -> None:
                log.info("Dev jar server: " + format, *args)

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="dmz-dev-jar-download",
            daemon=True,
        )
        self._thread.start()
        log.info(
            "Dev jar download server listening on %s:%s",
            self.host,
            self.port,
        )

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _submit_payload(self, payload: DevJarUploadPayload) -> None:
        future = asyncio.run_coroutine_threadsafe(self.on_payload(payload), self.loop)

        def log_result(done_future: asyncio.Future[None]) -> None:
            try:
                done_future.result()
            except Exception:
                log.exception("Dev jar upload payload handling failed")

        future.add_done_callback(log_result)

    def _run_oauth_callback(self, code: str, state: str) -> DevJarHttpResponse:
        if not code or not state:
            return text_response(400, "Missing OAuth code or state")
        future = asyncio.run_coroutine_threadsafe(
            self.on_oauth_callback(code, state),
            self.loop,
        )
        try:
            return future.result(timeout=30)
        except Exception:
            log.exception("Dev jar OAuth callback handling failed")
            return text_response(500, "Download authorization failed")
