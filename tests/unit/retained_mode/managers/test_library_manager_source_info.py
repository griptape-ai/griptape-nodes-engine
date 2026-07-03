import asyncio
from importlib.machinery import ModuleSpec
from pathlib import Path
from unittest.mock import patch

import pytest

from griptape_nodes.retained_mode.events.library_events import (
    GetEngineSourceInfoRequest,
    GetEngineSourceInfoResultFailure,
    GetEngineSourceInfoResultSuccess,
    GetLibrarySourceInfoRequest,
    GetLibrarySourceInfoResultFailure,
    GetLibrarySourceInfoResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager

# Compute the filesystem root once at import time so async test bodies don't
# call Path methods (ASYNC240 forbids Path I/O inside async functions).
_FS_ROOT = Path("/").resolve()


class TestGetLibrarySourceInfoRequest:
    @pytest.mark.asyncio
    async def test_returns_success_with_correct_paths(self, griptape_nodes: GriptapeNodes) -> None:
        library_manager = griptape_nodes.LibraryManager()

        root = _FS_ROOT
        json_path = str(root / "some" / "dir" / "griptape_nodes_library.json")
        dir_path = str(root / "some" / "dir")

        mock_lib_info = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
            fitness=LibraryManager.LibraryFitness.GOOD,
            library_path=json_path,
            is_sandbox=False,
            library_name="Test Library",
        )

        with patch.object(
            library_manager, "get_library_info_by_library_name", return_value=mock_lib_info, autospec=True
        ) as get_library_info_by_library_name:
            request = GetLibrarySourceInfoRequest(library="Test Library")
            result = await library_manager.on_get_library_source_info_request(request)

        get_library_info_by_library_name.assert_called_once_with("Test Library")
        assert isinstance(result, GetLibrarySourceInfoResultSuccess)
        assert result.library_name == "Test Library"
        assert result.library_json_path == json_path
        assert result.library_directory == dir_path

    @pytest.mark.asyncio
    async def test_library_directory_is_parent_of_json_path(self, griptape_nodes: GriptapeNodes) -> None:
        library_manager = griptape_nodes.LibraryManager()

        root = _FS_ROOT
        json_path = str(root / "libs" / "my_lib" / "griptape_nodes_library.json")

        mock_lib_info = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
            fitness=LibraryManager.LibraryFitness.GOOD,
            library_path=json_path,
            is_sandbox=False,
            library_name="My Lib",
        )

        with patch.object(
            library_manager, "get_library_info_by_library_name", return_value=mock_lib_info, autospec=True
        ) as get_library_info_by_library_name:
            request = GetLibrarySourceInfoRequest(library="My Lib")
            result = await library_manager.on_get_library_source_info_request(request)

        get_library_info_by_library_name.assert_called_once_with("My Lib")
        assert isinstance(result, GetLibrarySourceInfoResultSuccess)
        assert Path(result.library_directory) == Path(result.library_json_path).parent

    @pytest.mark.asyncio
    async def test_returns_failure_when_library_not_found(self, griptape_nodes: GriptapeNodes) -> None:
        library_manager = griptape_nodes.LibraryManager()

        with patch.object(
            library_manager, "get_library_info_by_library_name", return_value=None, autospec=True
        ) as get_library_info_by_library_name:
            request = GetLibrarySourceInfoRequest(library="NonexistentLib")
            result = await library_manager.on_get_library_source_info_request(request)

        get_library_info_by_library_name.assert_called_once_with("NonexistentLib")
        assert isinstance(result, GetLibrarySourceInfoResultFailure)

    @pytest.mark.asyncio
    async def test_waits_for_libraries_loading_complete(self, griptape_nodes: GriptapeNodes) -> None:
        library_manager = griptape_nodes.LibraryManager()

        loading_event = asyncio.Event()
        root = _FS_ROOT
        json_path = str(root / "some" / "dir" / "griptape_nodes_library.json")
        mock_lib_info = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
            fitness=LibraryManager.LibraryFitness.GOOD,
            library_path=json_path,
            is_sandbox=False,
            library_name="Test Library",
        )

        with (
            patch.object(library_manager, "_libraries_loading_complete", loading_event),
            patch.object(
                library_manager,
                "get_library_info_by_library_name",
                return_value=mock_lib_info,
                autospec=True,
            ) as get_library_info_by_library_name,
        ):
            request = GetLibrarySourceInfoRequest(library="Test Library")
            task = asyncio.create_task(library_manager.on_get_library_source_info_request(request))

            await asyncio.sleep(0)
            assert not task.done()

            loading_event.set()
            result = await task

        get_library_info_by_library_name.assert_called_once_with("Test Library")
        assert isinstance(result, GetLibrarySourceInfoResultSuccess)


class TestGetEngineSourceInfoRequest:
    def test_returns_success_with_valid_directory(self, griptape_nodes: GriptapeNodes) -> None:
        library_manager = griptape_nodes.LibraryManager()

        request = GetEngineSourceInfoRequest()
        result = library_manager.on_get_engine_source_info_request(request)

        assert isinstance(result, GetEngineSourceInfoResultSuccess)
        assert Path(result.package_directory).is_dir()

    def test_package_directory_contains_init(self, griptape_nodes: GriptapeNodes) -> None:
        library_manager = griptape_nodes.LibraryManager()

        request = GetEngineSourceInfoRequest()
        result = library_manager.on_get_engine_source_info_request(request)

        assert isinstance(result, GetEngineSourceInfoResultSuccess)
        assert (Path(result.package_directory) / "__init__.py").exists()

    def test_package_directory_contains_exe_types(self, griptape_nodes: GriptapeNodes) -> None:
        library_manager = griptape_nodes.LibraryManager()

        request = GetEngineSourceInfoRequest()
        result = library_manager.on_get_engine_source_info_request(request)

        assert isinstance(result, GetEngineSourceInfoResultSuccess)
        assert (Path(result.package_directory) / "exe_types" / "node_types.py").exists()

    def test_returns_failure_when_spec_not_found(self, griptape_nodes: GriptapeNodes) -> None:
        library_manager = griptape_nodes.LibraryManager()

        with patch("importlib.util.find_spec", return_value=None):
            request = GetEngineSourceInfoRequest()
            result = library_manager.on_get_engine_source_info_request(request)

        assert isinstance(result, GetEngineSourceInfoResultFailure)

    def test_returns_failure_when_spec_origin_is_none(self, griptape_nodes: GriptapeNodes) -> None:
        library_manager = griptape_nodes.LibraryManager()

        spec_without_origin = ModuleSpec(name="griptape_nodes", loader=None, origin=None)

        with patch("importlib.util.find_spec", return_value=spec_without_origin):
            request = GetEngineSourceInfoRequest()
            result = library_manager.on_get_engine_source_info_request(request)

        assert isinstance(result, GetEngineSourceInfoResultFailure)
