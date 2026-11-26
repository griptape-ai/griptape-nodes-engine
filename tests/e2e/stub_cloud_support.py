"""A stub Griptape Cloud for the end-to-end tests that need one to refuse a call over budget."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.drivers.cloud_credentials import BASE_URL_SETTING_NAME
from griptape_nodes.utils.budget_refusal import BUDGET_EXCEEDED_CODE

if TYPE_CHECKING:
    from collections.abc import Iterator

REFUSED_PATH = "/api/refused"
"""Stub route that answers the way Cloud does when a HARD budget has no room."""

OK_PATH = "/api/ok"
"""Stub route that answers the call, standing in for one the budget does not cover."""

OK_BODY = {"ok": True}


def a_refusal_body() -> dict[str, Any]:
    """The 403 body Cloud's ``SpendHold.as_refusal()`` builds, with two budgets refusing.

    Two rather than one on purpose: "names every violated budget" is the requirement, and a
    single-budget fixture cannot tell a message that names them all from one that names the first.
    """
    return {
        "error": BUDGET_EXCEEDED_CODE,
        "message": "Budget limit reached (tight, frozen-one).",
        "blocked_by": [
            {
                "budget_id": "3f1c6b4e-0000-4000-8000-000000000001",
                "budget_name": "tight",
                "scope_type": "ORG",
                "reset_period": "MONTHLY",
                "enforcement": "HARD",
                "limit_credits": 100,
                "spent_credits": 90,
                "spent_by_cost_basis": {"billed": 90, "estimated": 0, "declared": 0},
                "includes_byok": False,
                "includes_reported": False,
                "remaining_credits": 10,
                "requested_credits": 50,
                "frozen": False,
            },
            {
                "budget_id": "3f1c6b4e-0000-4000-8000-000000000002",
                "budget_name": "frozen-one",
                "scope_type": "LICENSE",
                "reset_period": "DAILY",
                "enforcement": "HARD",
                "limit_credits": 10_000,
                "spent_credits": 0,
                "spent_by_cost_basis": {"billed": 0, "estimated": 0, "declared": 0},
                "includes_byok": False,
                "includes_reported": False,
                "remaining_credits": 10_000,
                "requested_credits": 50,
                "frozen": True,
            },
        ],
        "effective_remaining_credits": 10,
        "spend_id": "9a2d5e70-0000-4000-8000-00000000000f",
    }


class _StubCloudHandler(BaseHTTPRequestHandler):
    """Answers the routes these tests need and stays silent in the pytest output."""

    def do_GET(self) -> None:  # BaseHTTPRequestHandler's spelling
        if self.path == REFUSED_PATH:
            body = json.dumps(a_refusal_body()).encode()
            self.send_response(403)
        elif self.path == OK_PATH:
            body = json.dumps(OK_BODY).encode()
            self.send_response(200)
        else:
            body = json.dumps({"error": "something else went wrong"}).encode()
            self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002  # the base class names it this
        """Swallow the per-request line the base class writes to stderr."""


@pytest.fixture
def stub_cloud(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Run a stub Griptape Cloud and point the engine's host resolution at it.

    Setting ``GT_CLOUD_BASE_URL`` is not incidental: the refusal parser is host-scoped, so a
    workflow's calls to third-party APIs are never mistaken for Griptape budget refusals. That
    means a test using a stub host has to tell the engine the stub *is* Cloud, exactly as pointing
    the engine at a dev control plane would.
    """
    server = HTTPServer(("127.0.0.1", 0), _StubCloudHandler)
    host, port = server.server_address[0], server.server_address[1]
    base_url = f"http://{host}:{port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(BASE_URL_SETTING_NAME, base_url)
    try:
        yield base_url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
