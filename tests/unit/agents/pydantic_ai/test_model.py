"""HTTP timeouts on the models `build_model` returns."""

from __future__ import annotations

import asyncio
import socket
import threading
from typing import TYPE_CHECKING

import httpx
import pytest
from pydantic_ai import Agent

from griptape_nodes.agents.pydantic_ai.model import build_model
from griptape_nodes.drivers.cloud_models import ProviderID

if TYPE_CHECKING:
    from collections.abc import Iterator

# A TLS-inspecting proxy was measured holding a first handshake for ~28s.
_SLOW_PROXY_HANDSHAKE_SECONDS = 28.0
_PAST_SDK_DEFAULT_CONNECT_SECONDS = 7.0
_OPENAI_SDK_REQUEST_TIMEOUT_SECONDS = 600.0


@pytest.fixture
def stalled_tls_url() -> Iterator[str]:
    """An HTTPS URL whose server accepts TCP but never answers the TLS handshake."""
    server = socket.create_server(("127.0.0.1", 0))
    accepted: list[socket.socket] = []

    def accept_forever() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            accepted.append(conn)

    threading.Thread(target=accept_forever, daemon=True).start()
    yield f"https://127.0.0.1:{server.getsockname()[1]}/v1"
    server.close()
    for conn in accepted:
        conn.close()


@pytest.mark.parametrize(
    ("provider", "api_key", "base_url"),
    [
        (ProviderID.GRIPTAPE_CLOUD, "key", None),
        (ProviderID.OLLAMA, None, None),
        (ProviderID.LMSTUDIO, None, None),
        ("custom", "key", "https://example.invalid/v1"),
    ],
)
def test_every_provider_allows_a_slow_tls_handshake(provider: str, api_key: str | None, base_url: str | None) -> None:
    """Each provider's requests must outlast a proxy's slow first handshake."""
    model = build_model("gpt-4o", provider=provider, api_key=api_key, base_url=base_url, settings={"max_tokens": 10})

    settings = model.settings or {}
    timeout = settings.get("timeout")
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect is not None
    assert timeout.connect > _SLOW_PROXY_HANDSHAKE_SECONDS
    assert timeout.read == _OPENAI_SDK_REQUEST_TIMEOUT_SECONDS
    assert settings.get("max_tokens") == 10  # noqa: PLR2004


def test_caller_timeout_wins() -> None:
    """A ``timeout`` the caller sets replaces the default."""
    model = build_model(
        "gpt-4o", provider="custom", api_key="key", base_url="https://example.invalid/v1", settings={"timeout": 5}
    )

    assert (model.settings or {}).get("timeout") == 5  # noqa: PLR2004


@pytest.mark.asyncio
async def test_stalled_handshake_outlasts_the_sdk_default_connect_timeout(stalled_tls_url: str) -> None:
    """A real run keeps waiting on a stalled handshake instead of failing at 5s."""
    model = build_model("gpt-4o", provider="custom", api_key="key", base_url=stalled_tls_url, settings={})
    model.client.max_retries = 0

    # The OpenAI SDK's default gives up after 5s with "Request timed out";
    # a run still waiting on the handshake past that is what a slow proxy needs.
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(Agent(model).run("hi"), timeout=_PAST_SDK_DEFAULT_CONNECT_SECONDS)
