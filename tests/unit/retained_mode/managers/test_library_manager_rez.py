"""Tests for the rez integration points in LibraryManager.

Rez itself is never invoked: every rez_utils helper is patched at the library_manager
namespace, so these tests pin down how LibraryManager reacts to what rez reports.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.node_library.library_declarations import LibraryDependencyDeclaration
from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete
from griptape_nodes.retained_mode.events.base_events import ResultDetails
from griptape_nodes.retained_mode.events.library_events import (
    CheckLibraryUpdateRequest,
    CheckLibraryUpdateResultFailure,
    CheckLibraryUpdateResultSuccess,
    InstallLibraryDependenciesRequest,
    InstallLibraryDependenciesResultFailure,
    InstallLibraryDependenciesResultSuccess,
    LoadLibraryMetadataFromFileResultSuccess,
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultFailure,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.managers.library_manager import DependencyInstallError, LibraryManager
from griptape_nodes.retained_mode.managers.settings import LibraryRegistration

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine

LIBRARY_MANAGER_MODULE = "griptape_nodes.retained_mode.managers.library_manager"
LIBRARY_NAME = "Demo Library"
FAMILY = "griptape_nodes_library_demo"
LIBRARY_JSON = "/libs/demo/griptape_nodes_library.json"


def _library_info(
    *,
    library_name: str | None = LIBRARY_NAME,
    library_path: str = LIBRARY_JSON,
    registered_path: str | None = None,
    rez_version: str | None = None,
    lifecycle_state: LibraryManager.LibraryLifecycleState = LibraryManager.LibraryLifecycleState.LOADED,
) -> LibraryManager.LibraryInfo:
    return LibraryManager.LibraryInfo(
        lifecycle_state=lifecycle_state,
        fitness=LibraryManager.LibraryFitness.GOOD,
        library_path=library_path,
        is_sandbox=False,
        library_name=library_name,
        registered_path=registered_path,
        rez_version=rez_version,
    )


def _metadata_success(schema: MagicMock, file_path: str = LIBRARY_JSON) -> LoadLibraryMetadataFromFileResultSuccess:
    return LoadLibraryMetadataFromFileResultSuccess(
        library_schema=schema,
        file_path=file_path,
        git_remote=None,
        git_ref=None,
        enabled=True,
        is_registered=False,
        result_details=ResultDetails(message="OK", level=20),
    )


def _schema_with_dependencies(pip: list[str], pip_exec: list[str]) -> MagicMock:
    schema = MagicMock()
    schema.name = LIBRARY_NAME
    schema.metadata.dependencies.pip_dependencies = pip
    schema.metadata.dependencies.pip_dependencies_exec = pip_exec
    schema.metadata.dependencies.pip_install_flags = []
    return schema


class TestRecordRezPackageInfo:
    def test_records_available_package(self, engine: Engine) -> None:
        info = _library_info()

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.library_file_path_to_rez_family", return_value=FAMILY),
            patch(f"{LIBRARY_MANAGER_MODULE}.get_library_rez_package_version", return_value="1.2.0") as mock_version,
        ):
            engine.library_manager._record_rez_package_info(info)

        assert info.rez_family == FAMILY
        assert info.rez_version == "1.2.0"
        assert info.has_rez_package is True
        assert mock_version.call_args.args[0] == LIBRARY_NAME
        assert mock_version.call_args.kwargs["library_file_path"] == Path(LIBRARY_JSON)

    def test_records_package_not_built(self, engine: Engine) -> None:
        info = _library_info()

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.library_file_path_to_rez_family", return_value=FAMILY),
            patch(f"{LIBRARY_MANAGER_MODULE}.get_library_rez_package_version", return_value=None),
        ):
            engine.library_manager._record_rez_package_info(info)

        assert info.rez_family == FAMILY
        assert info.rez_version is None
        assert info.has_rez_package is False

    def test_without_a_library_path_records_nothing(self, engine: Engine) -> None:
        info = _library_info(library_path="")

        with patch(f"{LIBRARY_MANAGER_MODULE}.get_library_rez_package_version") as mock_version:
            engine.library_manager._record_rez_package_info(info)

        mock_version.assert_not_called()
        assert info.rez_family is None
        assert info.has_rez_package is False

    @pytest.mark.parametrize("library_name", [None, ""])
    def test_unnamed_library_is_a_no_op(self, engine: Engine, library_name: str | None) -> None:
        info = _library_info(library_name=library_name)

        with patch(f"{LIBRARY_MANAGER_MODULE}.library_file_path_to_rez_family") as mock_family:
            engine.library_manager._record_rez_package_info(info)

        mock_family.assert_not_called()


class TestRegisterLibraryRecordsRezInfo:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("rez_enabled", [True, False])
    async def test_records_only_when_rez_enabled(self, engine: Engine, *, rez_enabled: bool) -> None:
        mgr = engine.library_manager
        info = _library_info()
        prerequisites = LibraryManager.RegisterLibraryPrerequisites(library_info=info, file_path=LIBRARY_JSON)

        with (
            patch.object(mgr, "_establish_register_library_prerequisites", AsyncMock(return_value=prerequisites)),
            patch.object(mgr, "_progress_library_through_lifecycle", AsyncMock(return_value=None)),
            patch(f"{LIBRARY_MANAGER_MODULE}.is_rez_enabled", return_value=rez_enabled),
            patch.object(mgr, "_record_rez_package_info") as mock_record,
        ):
            result = await mgr.register_library_from_file_request(
                RegisterLibraryFromFileRequest(file_path=LIBRARY_JSON)
            )

        assert isinstance(result, RegisterLibraryFromFileResultSuccess)
        if rez_enabled:
            mock_record.assert_called_once_with(info)
        else:
            mock_record.assert_not_called()


class TestCheckLibraryUpdateRez:
    async def _check(
        self, engine: Engine, info: LibraryManager.LibraryInfo, *, rez_enabled: bool, latest: str | None
    ) -> tuple[object, MagicMock]:
        mgr = engine.library_manager
        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.is_rez_enabled", return_value=rez_enabled),
            patch.object(mgr, "get_library_info_by_library_name", return_value=info),
            patch(f"{LIBRARY_MANAGER_MODULE}.get_library_rez_package_version", return_value=latest) as mock_latest,
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", side_effect=KeyError(LIBRARY_NAME)),
        ):
            result = await mgr.check_library_update_request(CheckLibraryUpdateRequest(library_name=LIBRARY_NAME))
        return result, mock_latest

    @pytest.mark.asyncio
    async def test_pinned_package_never_reports_an_update(self, engine: Engine) -> None:
        info = _library_info(registered_path=f"REZ:{FAMILY}-1.0.0", rez_version="1.0.0")

        result, _ = await self._check(engine, info, rez_enabled=True, latest="2.0.0")

        assert isinstance(result, CheckLibraryUpdateResultSuccess)
        assert result.has_update is False
        assert result.rez_managed is True
        assert result.current_version == "1.0.0"
        assert result.latest_version == "1.0.0"

    @pytest.mark.asyncio
    async def test_unpinned_package_reports_newer_store_version(self, engine: Engine) -> None:
        info = _library_info(registered_path=f"REZ:{FAMILY}", rez_version="1.0.0")

        result, _ = await self._check(engine, info, rez_enabled=True, latest="2.0.0")

        assert isinstance(result, CheckLibraryUpdateResultSuccess)
        assert result.has_update is True
        assert result.rez_managed is True
        assert result.latest_version == "2.0.0"
        assert result.git_remote is None

    @pytest.mark.asyncio
    async def test_unpinned_package_up_to_date(self, engine: Engine) -> None:
        info = _library_info(registered_path=f"REZ:{FAMILY}", rez_version="1.0.0")

        result, _ = await self._check(engine, info, rez_enabled=True, latest="1.0.0")

        assert isinstance(result, CheckLibraryUpdateResultSuccess)
        assert result.has_update is False
        assert "Up to date" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_rez_disabled_takes_the_git_path(self, engine: Engine) -> None:
        info = _library_info(registered_path=f"REZ:{FAMILY}", rez_version="1.0.0")

        result, mock_latest = await self._check(engine, info, rez_enabled=False, latest="2.0.0")

        mock_latest.assert_not_called()
        # The git path looks the library up in the registry, which this test makes fail.
        assert isinstance(result, CheckLibraryUpdateResultFailure)


class TestAddLibraryPathsToSysPathRez:
    async def _add_paths(
        self,
        engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        manifest: tuple[str, list[str], list[str], list[str]],
        resolved: list[str],
    ) -> tuple[list[str], MagicMock, AsyncMock]:
        fake_sys_path: list[str] = ["/existing"]
        monkeypatch.setattr(sys, "path", fake_sys_path)
        mgr = engine.library_manager
        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.is_rez_enabled", return_value=True),
            patch(f"{LIBRARY_MANAGER_MODULE}.is_library_rez_package_available", return_value=True),
            patch(f"{LIBRARY_MANAGER_MODULE}.library_file_path_to_rez_family", return_value=FAMILY),
            patch(f"{LIBRARY_MANAGER_MODULE}.read_library_manifest", return_value=manifest),
            patch(f"{LIBRARY_MANAGER_MODULE}.resolve_rez_pythonpath", return_value=resolved) as mock_resolve,
            patch.object(mgr, "_add_library_edit_venv_to_sys_path", AsyncMock()) as mock_venv,
        ):
            await mgr._add_library_paths_to_sys_path(LIBRARY_NAME, LIBRARY_JSON, tmp_path)
        return fake_sys_path, mock_resolve, mock_venv

    @pytest.mark.asyncio
    async def test_exec_deps_resolve_only_edit_time_packages(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        manifest = (LIBRARY_NAME, ["Pillow>=10", "ruamel.yaml"], ["torch==2.7.0"], [])

        sys_path, mock_resolve, mock_venv = await self._add_paths(
            engine, monkeypatch, tmp_path, manifest=manifest, resolved=["/rez/pillow/python"]
        )

        mock_resolve.assert_called_once_with(["pillow", "ruamel_yaml"])
        assert sys_path[0] == "/rez/pillow/python"
        assert str(tmp_path) in sys_path
        mock_venv.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exec_deps_without_edit_deps_resolve_nothing(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        manifest = (LIBRARY_NAME, [], ["torch==2.7.0"], [])

        sys_path, mock_resolve, _ = await self._add_paths(engine, monkeypatch, tmp_path, manifest=manifest, resolved=[])

        mock_resolve.assert_not_called()
        assert sys_path == [str(tmp_path), "/existing"]

    @pytest.mark.asyncio
    async def test_no_exec_deps_resolve_the_whole_family(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        manifest = (LIBRARY_NAME, ["pillow"], [], [])
        resolved = ["/rez/a/python", "/existing", "/rez/a/python"]

        sys_path, mock_resolve, _ = await self._add_paths(
            engine, monkeypatch, tmp_path, manifest=manifest, resolved=resolved
        )

        mock_resolve.assert_called_once_with([FAMILY])
        # Each path is inserted once, and paths already on sys.path are left where they are.
        assert sys_path.count("/rez/a/python") == 1
        assert sys_path.count("/existing") == 1

    @pytest.mark.asyncio
    async def test_non_rez_library_uses_the_edit_venv(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(sys, "path", ["/existing"])
        mgr = engine.library_manager
        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.is_rez_enabled", return_value=False),
            patch(f"{LIBRARY_MANAGER_MODULE}.resolve_rez_pythonpath") as mock_resolve,
            patch.object(mgr, "_add_library_edit_venv_to_sys_path", AsyncMock()) as mock_venv,
        ):
            await mgr._add_library_paths_to_sys_path(LIBRARY_NAME, LIBRARY_JSON, tmp_path)

        mock_resolve.assert_not_called()
        mock_venv.assert_awaited_once_with(LIBRARY_NAME, LIBRARY_JSON)


class TestResolveDiscoveryPathRez:
    def test_rez_entry_resolves_to_the_package_manifest(self, tmp_path: Path) -> None:
        manifest = tmp_path / "griptape_nodes_library.json"
        entry = LibraryRegistration(path=f"REZ:{FAMILY}-1.2.0")

        with patch(f"{LIBRARY_MANAGER_MODULE}.resolve_rez_library_json_path", return_value=manifest) as mock_resolve:
            resolved = LibraryManager._resolve_discovery_path(entry, tmp_path)

        mock_resolve.assert_called_once_with(FAMILY, version="1.2.0")
        assert resolved is not None
        assert resolved.path == manifest
        assert resolved.registered_path == f"REZ:{FAMILY}-1.2.0"

    def test_missing_rez_package_resolves_to_none(self, tmp_path: Path) -> None:
        entry = LibraryRegistration(path=f"REZ:{FAMILY}")

        with patch(f"{LIBRARY_MANAGER_MODULE}.resolve_rez_library_json_path", return_value=None) as mock_resolve:
            resolved = LibraryManager._resolve_discovery_path(entry, tmp_path)

        mock_resolve.assert_called_once_with(FAMILY, version=None)
        assert resolved is None


class TestInstallDependenciesRez:
    @pytest.mark.asyncio
    async def test_rez_entry_skips_the_venv_build(self, engine: Engine) -> None:
        mgr = engine.library_manager
        schema = _schema_with_dependencies(["pillow"], ["torch"])
        rez_path = f"REZ:{FAMILY}"

        with (
            patch.object(mgr, "load_library_metadata_from_file_request", return_value=_metadata_success(schema)),
            patch.object(mgr, "_install_dependency_set", AsyncMock()) as mock_install,
        ):
            result = await mgr.install_library_dependencies_request(
                InstallLibraryDependenciesRequest(library_file_path=rez_path)
            )

        assert isinstance(result, InstallLibraryDependenciesResultSuccess)
        assert result.dependencies_installed == 0
        mock_install.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_library_with_rez_package_skips_the_venv_build(self, engine: Engine) -> None:
        mgr = engine.library_manager
        schema = _schema_with_dependencies(["pillow"], [])

        with (
            patch.object(mgr, "load_library_metadata_from_file_request", return_value=_metadata_success(schema)),
            patch(f"{LIBRARY_MANAGER_MODULE}.is_rez_enabled", return_value=True),
            patch(f"{LIBRARY_MANAGER_MODULE}.is_library_rez_package_available", return_value=True) as mock_available,
            patch.object(mgr, "_install_dependency_set", AsyncMock()) as mock_install,
        ):
            result = await mgr.install_library_dependencies_request(
                InstallLibraryDependenciesRequest(library_file_path=LIBRARY_JSON)
            )

        assert isinstance(result, InstallLibraryDependenciesResultSuccess)
        assert result.dependencies_installed == 0
        assert mock_available.call_args.kwargs["library_file_path"] == Path(LIBRARY_JSON)
        mock_install.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rez_enabled_still_reports_env_build_failures(self, engine: Engine, tmp_path: Path) -> None:
        """A library with no rez package still builds its venv; the rez log line must not change that."""
        mgr = engine.library_manager

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.is_rez_enabled", return_value=True),
            patch.object(mgr, "_get_library_venv_path", return_value=tmp_path / ".venv"),
            patch.object(mgr, "_init_library_venv", AsyncMock(side_effect=RuntimeError("no python"))),
            pytest.raises(DependencyInstallError, match="no python"),
        ):
            await mgr._install_dependency_set(
                library_name=LIBRARY_NAME,
                library_file_path=LIBRARY_JSON,
                pip_dependencies=["pillow"],
                pip_install_flags=[],
                execution=False,
            )


class TestLibraryDependencyFromRezStore:
    """A declared library dependency found in the rez store is registered from it, never downloaded."""

    _STOP = InstallLibraryDependenciesResultFailure(result_details="stop-sentinel")

    async def _progress(self, engine: Engine, *, register_result: object) -> tuple[AsyncMock, MagicMock]:
        mgr = engine.library_manager
        info = _library_info(library_path="/mock.json", lifecycle_state=LibraryManager.LibraryLifecycleState.EVALUATED)
        schema = MagicMock()
        schema.name = LIBRARY_NAME
        schema.metadata.declarations = [
            LibraryDependencyDeclaration(url="griptape-ai/griptape-nodes-library-opencolorio@v1.2.0")
        ]
        dep_json = Path("/rez/opencolorio/1.2.0/python/griptape_nodes_library.json")

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.is_rez_enabled", return_value=True),
            patch(f"{LIBRARY_MANAGER_MODULE}.resolve_rez_library_json_path", return_value=dep_json),
            patch.object(
                mgr, "load_library_metadata_from_file_request", return_value=_metadata_success(schema, "/mock.json")
            ),
            patch.object(mgr, "register_library_from_file_request", AsyncMock(return_value=register_result)) as reg,
            patch.object(mgr, "download_library_request") as mock_download,
            patch.object(mgr, "install_library_dependencies_request", return_value=self._STOP),
            patch.object(mgr, "_library_file_path_to_info", {"/mock.json": info}),
        ):
            await mgr._progress_library_through_lifecycle(
                library_info=info,
                file_path="/mock.json",
                request=RegisterLibraryFromFileRequest(file_path="/mock.json"),
            )
        return reg, mock_download

    @pytest.mark.asyncio
    async def test_registers_dependency_from_rez_store(self, engine: Engine) -> None:
        success = RegisterLibraryFromFileResultSuccess(library_name="opencolorio", result_details="ok")

        register, download = await self._progress(engine, register_result=success)

        register.assert_awaited_once()
        assert register.await_args is not None
        registered_path = register.await_args.args[0].file_path
        assert Path(registered_path) == Path("/rez/opencolorio/1.2.0/python/griptape_nodes_library.json")
        download.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_rez_registration_still_skips_download(self, engine: Engine) -> None:
        failure = RegisterLibraryFromFileResultFailure(result_details="bad package")

        register, download = await self._progress(engine, register_result=failure)

        register.assert_awaited_once()
        download.assert_not_called()


class TestAppInitializationRezHealthCheck:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("rez_enabled", [True, False])
    async def test_runs_startup_health_check_only_when_enabled(self, engine: Engine, *, rez_enabled: bool) -> None:
        mgr = engine.library_manager
        mock_engine = MagicMock()
        mock_engine.workflow_manager.refresh_workflow_registry = AsyncMock()

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.is_rez_enabled", return_value=rez_enabled),
            patch.object(mgr, "_engine", mock_engine),
        ):
            await mgr._run_app_initialization(AppInitializationComplete(skip_library_loading=True))

        if rez_enabled:
            mock_engine.rez_manager.run_startup_health_check.assert_called_once_with()
        else:
            mock_engine.rez_manager.run_startup_health_check.assert_not_called()
