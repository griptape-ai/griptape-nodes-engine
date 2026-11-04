"""Tests for how InspectLibraryRepoRequest picks the git ref to inspect."""

from __future__ import annotations

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
