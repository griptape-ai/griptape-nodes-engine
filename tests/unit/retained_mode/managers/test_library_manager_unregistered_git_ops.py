"""Tests that LibraryManager's git-backed handlers can operate on a library that never loaded.

A library whose manifest declares an engine_version above the running engine is marked UNUSABLE
and never reaches the LibraryRegistry, so switching it to an older ref is the only way an artist
can recover it. These tests pin down that the handlers resolve such a library from its on-disk
info rather than from the registry, which would make the repair unreachable for exactly the
libraries that need it.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileResultSuccess,
    SwitchLibraryRefRequest,
    SwitchLibraryRefResultFailure,
    SwitchLibraryRefResultSuccess,
    UnloadLibraryFromRegistryRequest,
)
from griptape_nodes.retained_mode.managers.library_manager import (
    LibraryGitOperationContext,
    LibraryManager,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine

LIBRARY_MANAGER_MODULE = "griptape_nodes.retained_mode.managers.library_manager"
LIBRARY_DIR = Path("/var/lib/test_lib")
MANIFEST_PATH = LIBRARY_DIR / "griptape_nodes_library.json"


def _unregistered_info(*, version: str | None = "0.88.0") -> LibraryManager.LibraryInfo:
    """Build the info recorded for a library that failed its engine-compatibility check."""
    return LibraryManager.LibraryInfo(
        lifecycle_state=LibraryManager.LibraryLifecycleState.FAILURE,
        fitness=LibraryManager.LibraryFitness.UNUSABLE,
        library_path=str(MANIFEST_PATH),
        is_sandbox=False,
        library_name="test_lib",
        library_version=version,
    )


class TestGitOperationValidationWithoutRegistration:
    """Test _validate_and_prepare_library_for_git_operation for an unregistered library."""

    @pytest.mark.asyncio
    async def test_validation_succeeds_for_library_that_never_loaded(self, engine: Engine) -> None:
        """A library absent from the registry still resolves, taking its version from on-disk info."""
        library_manager = engine.library_manager

        with patch.object(library_manager, "get_library_info_by_library_name", return_value=_unregistered_info()):
            result = await library_manager._validate_and_prepare_library_for_git_operation(
                library_name="test_lib",
                failure_result_class=SwitchLibraryRefResultFailure,
                operation_description="switch branch/tag for",
            )

        assert isinstance(result, LibraryGitOperationContext)
        assert result.old_version == "0.88.0"
        assert result.library_file_path == str(MANIFEST_PATH)
        assert result.library_dir == LIBRARY_DIR

    @pytest.mark.asyncio
    async def test_validation_fails_when_no_library_is_known(self, engine: Engine) -> None:
        """A name that matches nothing on disk is still a failure, not a git operation on nowhere."""
        library_manager = engine.library_manager

        with patch.object(library_manager, "get_library_info_by_library_name", return_value=None):
            result = await library_manager._validate_and_prepare_library_for_git_operation(
                library_name="test_lib",
                failure_result_class=SwitchLibraryRefResultFailure,
                operation_description="switch branch/tag for",
            )

        assert isinstance(result, SwitchLibraryRefResultFailure)
        assert "test_lib" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_validation_fails_without_a_version(self, engine: Engine) -> None:
        """A library whose metadata never parsed has no version to report as the old one."""
        library_manager = engine.library_manager

        with patch.object(
            library_manager, "get_library_info_by_library_name", return_value=_unregistered_info(version=None)
        ):
            result = await library_manager._validate_and_prepare_library_for_git_operation(
                library_name="test_lib",
                failure_result_class=SwitchLibraryRefResultFailure,
                operation_description="switch branch/tag for",
            )

        assert isinstance(result, SwitchLibraryRefResultFailure)


class TestSwitchLibraryRefWithoutRegistration:
    """Test switch_library_ref_request for an unregistered library."""

    @pytest.mark.asyncio
    async def test_switch_repairs_library_that_never_loaded(self, engine: Engine) -> None:
        """Switching an unregistered library to an older ref reaches git and reports the switch.

        Also covers the unload: unregistering a library that never reached the registry raises, so
        the reload has to skip it or the switch would fail after the working tree had moved.
        """
        library_manager = engine.library_manager
        handle_request = MagicMock()

        with (
            patch.object(library_manager, "get_library_info_by_library_name", return_value=_unregistered_info()),
            patch(f"{LIBRARY_MANAGER_MODULE}.get_current_ref", side_effect=["stable", "v0.87.0"]),
            patch(f"{LIBRARY_MANAGER_MODULE}.switch_branch_or_tag") as mock_switch,
            patch(f"{LIBRARY_MANAGER_MODULE}.find_file_in_directory", return_value=MANIFEST_PATH),
            patch.object(library_manager.engine, "handle_request", new=handle_request),
            patch.object(
                library_manager.engine,
                "ahandle_request",
                new=AsyncMock(return_value=MagicMock(spec=RegisterLibraryFromFileResultSuccess)),
            ),
        ):
            result = await library_manager.switch_library_ref_request(
                SwitchLibraryRefRequest(library_name="test_lib", ref_name="v0.87.0")
            )

        assert isinstance(result, SwitchLibraryRefResultSuccess)
        assert result.old_ref == "stable"
        assert result.new_ref == "v0.87.0"
        assert result.old_version == "0.88.0"
        mock_switch.assert_called_once_with(LIBRARY_DIR, "v0.87.0")
        assert not any(
            isinstance(call.args[0], UnloadLibraryFromRegistryRequest) for call in handle_request.call_args_list
        )
