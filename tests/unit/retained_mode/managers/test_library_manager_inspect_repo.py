"""Tests for InspectLibraryRepoRequest: which git ref it inspects, and where the checkout runs."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from griptape_nodes.retained_mode.events.library_events import (
    InspectLibraryRepoRequest,
    InspectLibraryRepoResultSuccess,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine

LIBRARY_MANAGER_MODULE = "griptape_nodes.retained_mode.managers.library.git_operations"
REPO_URL = "https://github.com/griptape-ai/griptape-nodes-library-nuke.git"


def _checkout() -> MagicMock:
    checkout = MagicMock()
    checkout.library_version = "1.0.0"
    checkout.commit_sha = "abcdef0123456789"
    checkout.library_data = {}
    return checkout


class TestInspectLibraryRepoRef:
    @pytest.mark.parametrize(
        ("git_url", "ref", "expected_ref"),
        [
            ("griptape-ai/griptape-nodes-library-nuke", None, "HEAD"),
            ("griptape-ai/griptape-nodes-library-nuke@stable", None, "stable"),
            ("griptape-ai/griptape-nodes-library-nuke@stable", "v1.2.0", "v1.2.0"),
            ("griptape-ai/griptape-nodes-library-nuke", "stable", "stable"),
        ],
    )
    @pytest.mark.asyncio
    async def test_resolves_ref(self, engine: Engine, git_url: str, ref: str | None, expected_ref: str) -> None:
        """A url@ref suffix is split off the URL, and an explicit ref overrides it."""
        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.sparse_checkout_library_json", return_value=_checkout()) as checkout,
            patch(f"{LIBRARY_MANAGER_MODULE}.LibrarySchema", return_value=MagicMock()),
        ):
            result = await engine.library_manager.git_operations.inspect_library_repo_request(
                InspectLibraryRepoRequest(git_url=git_url, ref=ref)
            )

        checkout.assert_called_once_with(REPO_URL, expected_ref)
        assert isinstance(result, InspectLibraryRepoResultSuccess)
        assert result.git_url == REPO_URL
        assert result.ref == expected_ref


class TestInspectLibraryRepoEventLoop:
    @pytest.mark.asyncio
    async def test_sparse_checkout_runs_off_the_event_loop_thread(self, engine: Engine) -> None:
        """The checkout's git subprocesses must not block the loop serving the editor connection."""
        loop_thread = threading.current_thread()
        checkout_threads: list[threading.Thread] = []

        def fake_checkout(_url: str, _ref: str) -> MagicMock:
            checkout_threads.append(threading.current_thread())
            return _checkout()

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.sparse_checkout_library_json", side_effect=fake_checkout),
            patch(f"{LIBRARY_MANAGER_MODULE}.LibrarySchema", return_value=MagicMock()),
        ):
            result = await engine.library_manager.git_operations.inspect_library_repo_request(
                InspectLibraryRepoRequest(git_url="griptape-ai/griptape-nodes-library-nuke")
            )

        assert isinstance(result, InspectLibraryRepoResultSuccess)
        assert len(checkout_threads) == 1
        assert checkout_threads[0] is not loop_thread
