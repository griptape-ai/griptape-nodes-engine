"""Tests for where a git change to a library prepares its widgets.

A download, update, or ref switch hands the new checkout to widget preparation before the library
loads, passing on whether the user allowed the build. A refused or failed preparation never fails
the git operation; it shows up as a warning in the result.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultDetails, ResultPayload
from griptape_nodes.retained_mode.events.library_events import (
    DownloadLibraryRequest,
    DownloadLibraryResultSuccess,
    PrepareLibraryWidgetsResultFailure,
    PrepareLibraryWidgetsResultSuccess,
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
    SwitchLibraryRefRequest,
    SwitchLibraryRefResultSuccess,
    UpdateLibraryRequest,
    UpdateLibraryResultSuccess,
    WidgetPreparationFailureReason,
)
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine

GIT_OPERATIONS_MODULE = "griptape_nodes.retained_mode.managers.library.git_operations"
LIBRARY_NAME = "Hooked Library"


def _write_manifest(library_dir: Path) -> Path:
    library_dir.mkdir(parents=True, exist_ok=True)
    manifest = library_dir / "griptape_nodes_library.json"
    manifest.write_text(json.dumps({"name": LIBRARY_NAME}), encoding="utf-8")
    return manifest


def _track(library_manager: LibraryManager, manifest: Path) -> None:
    library_manager._library_file_path_to_info[str(manifest)] = LibraryManager.LibraryInfo(
        lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
        fitness=LibraryManager.LibraryFitness.GOOD,
        library_path=str(manifest),
        is_sandbox=False,
        library_name=LIBRARY_NAME,
        library_version="1.0.0",
    )


def _refused() -> PrepareLibraryWidgetsResultFailure:
    return PrepareLibraryWidgetsResultFailure(
        library_name=LIBRARY_NAME,
        reason=WidgetPreparationFailureReason.NOT_AUTHORIZED,
        result_details="Attempted to build the widgets. Failed due to the build not being allowed.",
    )


def _prepared() -> PrepareLibraryWidgetsResultSuccess:
    return PrepareLibraryWidgetsResultSuccess(
        library_name=LIBRARY_NAME,
        revision="local",
        node_version="v22.0.0",
        npm_version="10.0.0",
        output="",
        result_details="Built the widgets.",
    )


def _warnings(result_details: ResultDetails | str) -> list[str]:
    assert isinstance(result_details, ResultDetails)
    return [detail.message for detail in result_details.result_details if detail.level == logging.WARNING]


class Recorder:
    """Records the order in which patched steps run."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def step(self, name: str, return_value: Any) -> AsyncMock:
        async def run(*_args: Any, **_kwargs: Any) -> Any:
            self.calls.append(name)
            return return_value

        return AsyncMock(side_effect=run)


@pytest.fixture
def manifest(engine: Engine, tmp_path: Path) -> Path:
    """A tracked library on disk whose git operations are patched per test."""
    manifest = _write_manifest(tmp_path / "hooked_library")
    _track(engine.library_manager, manifest)
    return manifest


@pytest.fixture
def recorder() -> Recorder:
    """Records the order of the patched steps."""
    return Recorder()


@pytest.fixture
def patched_reload(engine: Engine, recorder: Recorder) -> Iterator[AsyncMock]:
    """Stands in for reloading the library, recording when it runs."""
    reload = recorder.step("reload", "1.1.0")
    with patch.object(engine.library_manager.git_operations, "reload_library", new=reload):
        yield reload


def _patch_prepare(engine: Engine, recorder: Recorder, result: object) -> Any:
    return patch.object(
        engine.library_manager.widget_preparation, "prepare_after_checkout", new=recorder.step("prepare", result)
    )


class TestUpdate:
    @pytest.fixture
    def patched_git(self) -> Iterator[None]:
        with (
            patch(f"{GIT_OPERATIONS_MODULE}.is_monorepo", return_value=False),
            patch(f"{GIT_OPERATIONS_MODULE}.update_library_git"),
            patch(f"{GIT_OPERATIONS_MODULE}.is_on_tag", return_value=False),
        ):
            yield

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("patched_git", "patched_reload")
    async def test_prepares_after_the_update_and_before_the_reload(
        self, engine: Engine, manifest: Path, recorder: Recorder
    ) -> None:
        with (
            _patch_prepare(engine, recorder, _prepared()) as prepare,
            patch.object(
                engine.library_manager.git_operations, "_update_target_engine_failure", new=AsyncMock(return_value=None)
            ),
        ):
            result = await engine.ahandle_request(
                UpdateLibraryRequest(library_name=LIBRARY_NAME, allow_widget_build=True)
            )

        assert isinstance(result, UpdateLibraryResultSuccess), result.result_details
        assert recorder.calls == ["prepare", "reload"]
        prepare.assert_awaited_once_with(LIBRARY_NAME, manifest, allow_build_execution=True)
        assert _warnings(result.result_details) == []

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("patched_git", "patched_reload")
    async def test_a_refused_preparation_is_a_warning_not_a_failure(
        self, engine: Engine, manifest: Path, recorder: Recorder
    ) -> None:
        with (
            _patch_prepare(engine, recorder, _refused()) as prepare,
            patch.object(
                engine.library_manager.git_operations, "_update_target_engine_failure", new=AsyncMock(return_value=None)
            ),
        ):
            result = await engine.ahandle_request(UpdateLibraryRequest(library_name=LIBRARY_NAME))

        assert isinstance(result, UpdateLibraryResultSuccess), result.result_details
        prepare.assert_awaited_once_with(LIBRARY_NAME, manifest, allow_build_execution=False)
        warnings = _warnings(result.result_details)
        assert len(warnings) == 1
        assert "widgets" in warnings[0]
        assert "Successfully updated" in str(result.result_details)


class TestSwitch:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("patched_reload")
    async def test_prepares_after_the_switch_and_before_the_reload(
        self, engine: Engine, manifest: Path, recorder: Recorder
    ) -> None:
        with (
            patch(f"{GIT_OPERATIONS_MODULE}.get_current_ref", side_effect=["main", "v2"]),
            patch(f"{GIT_OPERATIONS_MODULE}.switch_branch_or_tag"),
            _patch_prepare(engine, recorder, _refused()) as prepare,
        ):
            result = await engine.ahandle_request(
                SwitchLibraryRefRequest(library_name=LIBRARY_NAME, ref_name="v2", allow_widget_build=True)
            )

        assert isinstance(result, SwitchLibraryRefResultSuccess), result.result_details
        assert recorder.calls == ["prepare", "reload"]
        prepare.assert_awaited_once_with(LIBRARY_NAME, manifest, allow_build_execution=True)
        assert len(_warnings(result.result_details)) == 1


class TestDownload:
    @pytest.fixture
    def patched_register(self, engine: Engine, recorder: Recorder) -> Iterator[AsyncMock]:
        original = engine.ahandle_request

        async def handle(request: RequestPayload) -> ResultPayload:
            if isinstance(request, RegisterLibraryFromFileRequest):
                recorder.calls.append("register")
                return MagicMock(spec=RegisterLibraryFromFileResultSuccess)
            return await original(request)

        with patch.object(engine, "ahandle_request", new=AsyncMock(side_effect=handle)) as mock:
            yield mock

    @pytest.mark.asyncio
    async def test_prepares_a_fresh_clone_before_registering_it(
        self, engine: Engine, tmp_path: Path, recorder: Recorder, patched_register: AsyncMock
    ) -> None:
        downloads = tmp_path / "downloads"

        def clone(_url: str, target_path: Path, _ref: str | None) -> None:
            _write_manifest(target_path)

        with (
            patch(f"{GIT_OPERATIONS_MODULE}.clone_repository", side_effect=clone),
            _patch_prepare(engine, recorder, _refused()) as prepare,
        ):
            result = await patched_register(
                DownloadLibraryRequest(
                    git_url="https://github.com/example/hooked-library",
                    download_directory=str(downloads),
                    allow_widget_build=True,
                )
            )

        assert isinstance(result, DownloadLibraryResultSuccess), result.result_details
        assert recorder.calls == ["prepare", "register"]
        prepare.assert_awaited_once_with(
            LIBRARY_NAME, downloads / "hooked-library" / "griptape_nodes_library.json", allow_build_execution=True
        )
        assert len(_warnings(result.result_details)) == 1

    @pytest.mark.asyncio
    async def test_an_existing_library_that_was_not_cloned_is_not_prepared(
        self, engine: Engine, tmp_path: Path, recorder: Recorder, patched_register: AsyncMock
    ) -> None:
        downloads = tmp_path / "downloads"
        _write_manifest(downloads / "hooked-library")

        with (
            patch(f"{GIT_OPERATIONS_MODULE}.clone_repository") as clone,
            _patch_prepare(engine, recorder, _prepared()) as prepare,
        ):
            result = await patched_register(
                DownloadLibraryRequest(
                    git_url="https://github.com/example/hooked-library",
                    download_directory=str(downloads),
                    fail_on_exists=False,
                    allow_widget_build=True,
                )
            )

        assert isinstance(result, DownloadLibraryResultSuccess), result.result_details
        clone.assert_not_called()
        prepare.assert_not_called()


class TestProvisioning:
    @pytest.mark.asyncio
    async def test_startup_downloads_never_allow_a_widget_build(self, engine: Engine, tmp_path: Path) -> None:
        """Provisioning runs with nobody there to agree to running a library's build tools."""
        handle = AsyncMock(return_value=MagicMock())
        with (
            patch.object(engine.config_manager, "resolved_libraries_root", return_value=tmp_path),
            patch.object(engine, "ahandle_request", new=handle),
        ):
            await engine.library_manager.provisioning.download_libraries_from_git_urls(
                ["https://github.com/example/hooked-library"]
            )

        downloads = [call.args[0] for call in handle.call_args_list if isinstance(call.args[0], DownloadLibraryRequest)]
        assert downloads
        assert all(request.allow_widget_build is False for request in downloads)

    def test_the_git_requests_default_to_not_building(self) -> None:
        assert DownloadLibraryRequest(git_url="https://github.com/example/x").allow_widget_build is False
        assert UpdateLibraryRequest(library_name=LIBRARY_NAME).allow_widget_build is False
        assert SwitchLibraryRefRequest(library_name=LIBRARY_NAME, ref_name="v1").allow_widget_build is False
