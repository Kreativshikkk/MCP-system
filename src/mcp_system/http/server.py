"""Loopback-first HTTP transport for provider interception routers."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .base import HTTPRequest, HTTPResponse


class MiddlewareHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        router: Any,
        *,
        allowed_hosts: set[str] | None = None,
        max_body_bytes: int = 1024 * 1024,
    ) -> None:
        self.router = router
        self.max_body_bytes = max_body_bytes
        host, port = server_address
        self.allowed_hosts = allowed_hosts or {
            host.casefold(),
            "localhost",
            "127.0.0.1",
            "[::1]",
        }
        super().__init__(server_address, _MiddlewareHandler)


class _MiddlewareHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MCPSystemHTTP/0.1"
    sys_version = ""

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_PATCH(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        try:
            if not self._valid_host():
                # Do not leave a rejected request body on a persistent HTTP/1.1
                # connection, where it would be parsed as the next request.
                raw_length = self.headers.get("Content-Length", "0")
                if raw_length.isdigit():
                    self.rfile.read(int(raw_length))
                self._send(_json_error(421, "Misdirected Request"))
                return
            if self.headers.get("Transfer-Encoding"):
                self._send(_json_error(400, "Transfer-Encoding is not supported"))
                return
            raw_length = self.headers.get("Content-Length", "0")
            try:
                content_length = int(raw_length)
            except ValueError:
                self._send(_json_error(400, "Invalid Content-Length"))
                return
            if content_length < 0:
                self._send(_json_error(400, "Invalid Content-Length"))
                return
            if content_length > self.server.max_body_bytes:  # type: ignore[attr-defined]
                self._send(_json_error(413, "Request body is too large"))
                return
            body = self.rfile.read(content_length) if content_length else b""
            target = urlsplit(self.path)
            request = HTTPRequest(
                method=self.command,
                path=target.path,
                query={
                    key: tuple(values)
                    for key, values in parse_qs(
                        target.query, keep_blank_values=True
                    ).items()
                },
                headers={key: value for key, value in self.headers.items()},
                body=body,
            )
            response = self.server.router.dispatch(request)  # type: ignore[attr-defined]
        except Exception:
            response = _json_error(500, "Internal Server Error")
        self._send(response)

    def _valid_host(self) -> bool:
        value = self.headers.get("Host", "")
        host = value.rsplit(":", 1)[0].casefold() if value else ""
        return host in self.server.allowed_hosts  # type: ignore[attr-defined]

    def _send(self, response: HTTPResponse) -> None:
        self.send_response(response.status)
        for key, value in response.headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(response.body)))
        self.end_headers()
        if response.body:
            self.wfile.write(response.body)

    def log_message(self, format: str, *args: object) -> None:
        # Stdout/stderr ownership belongs to the embedding harness.
        return None


def _json_error(status: int, message: str) -> HTTPResponse:
    return HTTPResponse(
        status,
        json.dumps({"message": message}, separators=(",", ":")).encode(),
        {
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "no-store",
        },
    )


def serve_http(
    router: Any,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    allowed_hosts: set[str] | None = None,
) -> None:
    with MiddlewareHTTPServer(
        (host, port), router, allowed_hosts=allowed_hosts
    ) as server:
        server.serve_forever()
