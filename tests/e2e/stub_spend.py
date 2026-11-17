"""A stub Griptape Cloud that records the usage reports an engine posts to it."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Iterator

SPEND_PATH = "/api/spend"
REPORT_WAIT_SECONDS = 10.0


class StubSpendServer(ThreadingHTTPServer):
    """Records every usage report posted to it, holding each reply for `reply_delay` seconds."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _StubSpendHandler)
        self.reports: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.reply_delay = 0.0

    @property
    def base_url(self) -> str:
        host, port = self.server_address[0], self.server_address[1]
        return f"http://{host}:{port}"

    def received(self) -> list[dict[str, Any]]:
        """The reports received so far, in the order they arrived."""
        with self.lock:
            return list(self.reports)

    async def wait_for_reports(self, count: int) -> list[dict[str, Any]]:
        """Wait for `count` reports, since the engine posts them from a background thread.

        Waits with `asyncio.sleep`, so the engine's event loop keeps running meanwhile.
        """
        deadline = time.monotonic() + REPORT_WAIT_SECONDS
        while time.monotonic() < deadline:
            reports = self.received()
            if len(reports) >= count:
                return reports
            await asyncio.sleep(0.05)
        return self.received()


@contextlib.contextmanager
def serve_stub_spend() -> Iterator[StubSpendServer]:
    """Run a stub spend server on a background thread for the length of the block."""
    server = StubSpendServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _StubSpendHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # BaseHTTPRequestHandler's spelling
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        server = cast("StubSpendServer", self.server)
        time.sleep(server.reply_delay)
        if self.path == SPEND_PATH:
            with server.lock:
                server.reports.append(body)
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002  # the base class names it this
        """Swallow the per-request line the base class writes to stderr."""
