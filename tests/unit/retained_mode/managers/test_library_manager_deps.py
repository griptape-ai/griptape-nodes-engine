"""Tests for inter-library dependency resolution (GH#4740)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.node_library.library_declarations import (
    LibraryDependencyDeclaration,
    SuggestedWorkerMode,
    WorkerMode,
)
from griptape_nodes.node_library.library_registry import (
    Dependencies,
    LibraryNameAndVersion,
    LibrarySchema,
)
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.base_events import ResultDetails
from griptape_nodes.retained_mode.events.library_events import (
    DownloadLibraryRequest,
    DownloadLibraryResultFailure,
    DownloadLibraryResultSuccess,
    InstallLibraryDependenciesResultFailure,
    LoadLibraryMetadataFromFileResultSuccess,
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultFailure,
)
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import (
    DependencyInstallationFailedProblem,
    LibraryDependencyProblem,
)
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager
from griptape_nodes.retained_mode.managers.settings import LibraryDependencyInstallBehavior


class TestLibraryDependencyDeclaration:
    """Tests for the LibraryDependencyDeclaration declaration type."""

    def test_required_dep(self) -> None:
        decl = LibraryDependencyDeclaration(url="griptape-ai/griptape-nodes-library-opencolorio@v1.2.0")
        assert decl.url == "griptape-ai/griptape-nodes-library-opencolorio@v1.2.0"
        assert decl.required is True

    def test_optional_dep(self) -> None:
        decl = LibraryDependencyDeclaration(url="griptape-ai/griptape-nodes-library-opencolorio@v1.2.0", required=False)
        assert decl.required is False

    def test_round_trips_as_library_declaration(self) -> None:
        """LibraryDependencyDeclaration serializes/deserializes correctly via the discriminated union."""
        from griptape_nodes.node_library.library_registry import LibraryMetadata

        meta = LibraryMetadata.model_validate(
            {
                "author": "test",
                "description": "test",
                "library_version": "1.0.0",
                "engine_version": "0.10.0",
                "tags": [],
                "declarations": [
                    {"type": "library_dependency", "url": "griptape-ai/lib-a@v1.0.0", "required": True},
                    {"type": "library_dependency", "url": "griptape-ai/lib-b@v2.0.0", "required": False},
                ],
            }
        )
        lib_deps = [d for d in meta.declarations if isinstance(d, LibraryDependencyDeclaration)]
        assert lib_deps[0].url == "griptape-ai/lib-a@v1.0.0"
        assert lib_deps[0].required is True
        assert lib_deps[1].required is False

    def test_dependencies_has_no_library_dependencies_field(self) -> None:
        deps = Dependencies()
        assert not hasattr(deps, "library_dependencies")

    def test_schema_version_bumped(self) -> None:
        # 0.12.0 added Dependencies.pip_dependencies_exec; 0.13.0 added execution_modules.
        assert LibrarySchema.LATEST_SCHEMA_VERSION == "0.13.0"


class TestLibraryDependencyProblem:
    """Tests for the LibraryDependencyProblem fitness problem."""

    def test_single_problem_message(self) -> None:
        problem = LibraryDependencyProblem(
            dependency_name="griptape-ai/griptape-nodes-library-opencolorio@v1.2.0",
            error_message="Clone failed",
        )
        msg = LibraryDependencyProblem.collate_problems_for_display([problem])
        assert "griptape-ai/griptape-nodes-library-opencolorio@v1.2.0" in msg
        assert "Clone failed" in msg

    def test_multiple_problems_message_includes_errors(self) -> None:
        problems = [
            LibraryDependencyProblem(dependency_name="dep-a@v1", error_message="err1"),
            LibraryDependencyProblem(dependency_name="dep-b@v2", error_message="err2"),
        ]
        msg = LibraryDependencyProblem.collate_problems_for_display(problems)
        assert "dep-a@v1" in msg
        assert "dep-b@v2" in msg
        assert "err1" in msg
        assert "err2" in msg


def _make_lib_info() -> LibraryManager.LibraryInfo:
    """Create a LibraryInfo in EVALUATED state ready for the dep-resolution step."""
    return LibraryManager.LibraryInfo(
        lifecycle_state=LibraryManager.LibraryLifecycleState.EVALUATED,
        library_path="/mock.json",
        is_sandbox=False,
        library_name="test_lib",
        library_version="1.0.0",
        fitness=LibraryManager.LibraryFitness.GOOD,
        problems=[],
    )


def _make_schema_mock(library_dependencies: list[str] | None, *, optional: bool = False) -> MagicMock:
    schema = MagicMock()
    schema.name = "test_lib"
    schema.metadata.library_version = "1.0.0"
    schema.metadata.declarations = (
        [LibraryDependencyDeclaration(url=url, required=not optional) for url in library_dependencies]
        if library_dependencies is not None
        else []
    )
    return schema


def _metadata_success(schema: MagicMock) -> LoadLibraryMetadataFromFileResultSuccess:
    return LoadLibraryMetadataFromFileResultSuccess(
        library_schema=schema,
        file_path="/mock.json",
        git_remote=None,
        git_ref=None,
        enabled=True,
        is_registered=False,
        result_details=ResultDetails(message="OK", level=20),
    )


# Sentinel failure used to stop the lifecycle after EVALUATED without entering the LOADED step.
_INSTALL_STOP = InstallLibraryDependenciesResultFailure(result_details="stop-sentinel")


class TestLibraryDependencyResolution:
    """Tests for library dependency resolution in the EVALUATED lifecycle step.

    Each test drives _progress_library_through_lifecycle with a LibraryInfo pre-set
    to EVALUATED and mocks install_library_dependencies_request to return failure so
    the lifecycle stops cleanly after the dependency-resolution block, without needing
    to mock the full LOADED phase (node imports, LibraryRegistry, sys.path, etc.).
    """

    @pytest.mark.asyncio
    async def test_no_library_dependencies_skips_download(self, engine: Engine) -> None:
        """A library with no library_dependencies does not call download_library_request."""
        mgr = engine.library_manager
        lib_info = _make_lib_info()

        with (
            patch.object(
                mgr, "load_library_metadata_from_file_request", return_value=_metadata_success(_make_schema_mock(None))
            ),
            patch.object(mgr, "install_library_dependencies_request", return_value=_INSTALL_STOP),
            patch.object(mgr, "download_library_request") as mock_download,
            patch.object(mgr, "_library_file_path_to_info", {"/mock.json": lib_info}),
        ):
            await mgr._progress_library_through_lifecycle(
                library_info=lib_info,
                file_path="/mock.json",
                request=RegisterLibraryFromFileRequest(file_path="/mock.json"),
            )

        mock_download.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_library_dependencies_skips_download(self, engine: Engine) -> None:
        """A library with library_dependencies=[] does not call download_library_request."""
        mgr = engine.library_manager
        lib_info = _make_lib_info()

        with (
            patch.object(
                mgr, "load_library_metadata_from_file_request", return_value=_metadata_success(_make_schema_mock([]))
            ),
            patch.object(mgr, "install_library_dependencies_request", return_value=_INSTALL_STOP),
            patch.object(mgr, "download_library_request") as mock_download,
            patch.object(mgr, "_library_file_path_to_info", {"/mock.json": lib_info}),
        ):
            await mgr._progress_library_through_lifecycle(
                library_info=lib_info,
                file_path="/mock.json",
                request=RegisterLibraryFromFileRequest(file_path="/mock.json"),
            )

        mock_download.assert_not_called()

    @pytest.mark.asyncio
    async def test_already_tracked_dependency_skips_download(self, engine: Engine) -> None:
        """If the dep repo name appears in an existing tracked path with healthy state, download is skipped."""
        mgr = engine.library_manager
        lib_info = _make_lib_info()
        schema = _make_schema_mock(["griptape-ai/griptape-nodes-library-opencolorio@v1.2.0"])

        existing_dep_info = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
            library_path="/workspace/libraries/griptape-nodes-library-opencolorio/griptape_nodes_library.json",
            is_sandbox=False,
            library_name="griptape-nodes-library-opencolorio",
            library_version="1.2.0",
            fitness=LibraryManager.LibraryFitness.GOOD,
            problems=[],
        )
        existing_paths = {
            "/workspace/libraries/griptape-nodes-library-opencolorio/griptape_nodes_library.json": existing_dep_info,
            "/mock.json": lib_info,
        }

        with (
            patch.object(mgr, "load_library_metadata_from_file_request", return_value=_metadata_success(schema)),
            patch.object(mgr, "install_library_dependencies_request", return_value=_INSTALL_STOP),
            patch.object(mgr, "download_library_request") as mock_download,
            patch.object(mgr, "_library_file_path_to_info", existing_paths),
        ):
            await mgr._progress_library_through_lifecycle(
                library_info=lib_info,
                file_path="/mock.json",
                request=RegisterLibraryFromFileRequest(file_path="/mock.json"),
            )

        mock_download.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_dependency_triggers_download(self, engine: Engine) -> None:
        """A dep not yet tracked causes download_library_request to be called with correct args."""
        mgr = engine.library_manager
        lib_info = _make_lib_info()
        schema = _make_schema_mock(["griptape-ai/nodes-dep@v1.0.0"])

        with (
            patch.object(mgr, "load_library_metadata_from_file_request", return_value=_metadata_success(schema)),
            patch.object(mgr, "install_library_dependencies_request", return_value=_INSTALL_STOP),
            patch.object(
                mgr,
                "download_library_request",
                new_callable=AsyncMock,
                return_value=DownloadLibraryResultSuccess(
                    library_name="nodes-dep",
                    library_path="/workspace/libraries/nodes-dep/griptape_nodes_library.json",
                    result_details="Downloaded",
                ),
            ) as mock_download,
            patch.object(mgr, "_library_file_path_to_info", {"/mock.json": lib_info}),
        ):
            await mgr._progress_library_through_lifecycle(
                library_info=lib_info,
                file_path="/mock.json",
                request=RegisterLibraryFromFileRequest(file_path="/mock.json"),
            )

        mock_download.assert_called_once()
        req = mock_download.call_args[0][0]
        assert req.git_url == "https://github.com/griptape-ai/nodes-dep.git"
        assert req.branch_tag_commit == "v1.0.0"
        assert req.fail_on_exists is False
        assert req.auto_register is True

    @pytest.mark.asyncio
    async def test_dependency_failure_marks_library_unusable(self, engine: Engine) -> None:
        """When a dependency download fails, the library gets LibraryDependencyProblem and UNUSABLE fitness."""
        mgr = engine.library_manager
        lib_info = _make_lib_info()
        schema = _make_schema_mock(["griptape-ai/nodes-bad@v1.0.0"])

        with (
            patch.object(mgr, "load_library_metadata_from_file_request", return_value=_metadata_success(schema)),
            patch.object(mgr, "install_library_dependencies_request") as mock_install,
            patch.object(
                mgr,
                "download_library_request",
                new_callable=AsyncMock,
                return_value=DownloadLibraryResultFailure(result_details="Clone failed"),
            ),
            patch.object(mgr, "_library_file_path_to_info", {"/mock.json": lib_info}),
        ):
            result = await mgr._progress_library_through_lifecycle(
                library_info=lib_info,
                file_path="/mock.json",
                request=RegisterLibraryFromFileRequest(file_path="/mock.json"),
            )

        assert isinstance(result, RegisterLibraryFromFileResultFailure)
        mock_install.assert_not_called()
        assert lib_info.fitness == LibraryManager.LibraryFitness.UNUSABLE
        dep_problems = [p for p in lib_info.problems if isinstance(p, LibraryDependencyProblem)]
        assert len(dep_problems) == 1
        assert "griptape-ai/nodes-bad@v1.0.0" in dep_problems[0].dependency_name

    @pytest.mark.asyncio
    async def test_dependency_resolved_before_pip_install(self, engine: Engine) -> None:
        """Library dependency download happens before pip package installation."""
        mgr = engine.library_manager
        lib_info = _make_lib_info()
        schema = _make_schema_mock(["griptape-ai/nodes-dep@v1.0.0"])

        call_order: list[str] = []

        async def mock_download(_request: object) -> DownloadLibraryResultSuccess:
            call_order.append("download")
            return DownloadLibraryResultSuccess(
                library_name="nodes-dep",
                library_path="/workspace/libraries/nodes-dep/griptape_nodes_library.json",
                result_details="Downloaded",
            )

        async def mock_install(_request: object) -> InstallLibraryDependenciesResultFailure:
            call_order.append("install")
            return _INSTALL_STOP

        with (
            patch.object(mgr, "load_library_metadata_from_file_request", return_value=_metadata_success(schema)),
            patch.object(mgr, "install_library_dependencies_request", side_effect=mock_install),
            patch.object(mgr, "download_library_request", side_effect=mock_download),
            patch.object(mgr, "_library_file_path_to_info", {"/mock.json": lib_info}),
        ):
            await mgr._progress_library_through_lifecycle(
                library_info=lib_info,
                file_path="/mock.json",
                request=RegisterLibraryFromFileRequest(file_path="/mock.json"),
            )

        assert "download" in call_order
        assert "install" in call_order
        assert call_order.index("download") < call_order.index("install")

    @pytest.mark.asyncio
    async def test_never_behavior_skips_required_dep_and_marks_flawed(self, engine: Engine) -> None:
        """When install behavior is 'never', required deps are skipped and library is FLAWED."""
        mgr = engine.library_manager
        lib_info = _make_lib_info()
        schema = _make_schema_mock(["griptape-ai/nodes-dep@v1.0.0"])

        config_mock = MagicMock()
        config_mock.get_config_value.return_value = LibraryDependencyInstallBehavior.NEVER

        with (
            patch.object(mgr, "load_library_metadata_from_file_request", return_value=_metadata_success(schema)),
            patch.object(mgr, "install_library_dependencies_request", return_value=_INSTALL_STOP),
            patch.object(mgr, "download_library_request") as mock_download,
            patch.object(mgr, "_library_file_path_to_info", {"/mock.json": lib_info}),
            patch.object(engine, "_config_manager", config_mock),
        ):
            await mgr._progress_library_through_lifecycle(
                library_info=lib_info,
                file_path="/mock.json",
                request=RegisterLibraryFromFileRequest(file_path="/mock.json"),
            )

        mock_download.assert_not_called()
        assert lib_info.fitness == LibraryManager.LibraryFitness.FLAWED
        dep_problems = [p for p in lib_info.problems if isinstance(p, LibraryDependencyProblem)]
        assert len(dep_problems) == 1
        assert "nodes-dep" in dep_problems[0].dependency_name

    @pytest.mark.asyncio
    async def test_never_behavior_skips_optional_dep_without_problem(self, engine: Engine) -> None:
        """When install behavior is 'never', optional deps are silently skipped."""
        mgr = engine.library_manager
        lib_info = _make_lib_info()
        schema = _make_schema_mock(["griptape-ai/nodes-dep@v1.0.0"], optional=True)

        config_mock = MagicMock()
        config_mock.get_config_value.return_value = LibraryDependencyInstallBehavior.NEVER

        with (
            patch.object(mgr, "load_library_metadata_from_file_request", return_value=_metadata_success(schema)),
            patch.object(mgr, "install_library_dependencies_request", return_value=_INSTALL_STOP),
            patch.object(mgr, "download_library_request") as mock_download,
            patch.object(mgr, "_library_file_path_to_info", {"/mock.json": lib_info}),
            patch.object(engine, "_config_manager", config_mock),
        ):
            await mgr._progress_library_through_lifecycle(
                library_info=lib_info,
                file_path="/mock.json",
                request=RegisterLibraryFromFileRequest(file_path="/mock.json"),
            )

        mock_download.assert_not_called()
        assert lib_info.fitness == LibraryManager.LibraryFitness.GOOD
        dep_problems = [p for p in lib_info.problems if isinstance(p, LibraryDependencyProblem)]
        assert len(dep_problems) == 0

    @pytest.mark.asyncio
    async def test_optional_dep_failure_does_not_fail_registration(self, engine: Engine) -> None:
        """When an optional dep download fails, the lifecycle continues past the dep block.

        Unlike a required dep failure (which returns early before pip install), an optional
        dep failure only logs a warning. The lifecycle proceeds to install_library_dependencies,
        so mock_install must be called and no LibraryDependencyProblem must be recorded.
        """
        mgr = engine.library_manager
        lib_info = _make_lib_info()
        schema = _make_schema_mock(["griptape-ai/nodes-optional@v1.0.0"], optional=True)

        with (
            patch.object(mgr, "load_library_metadata_from_file_request", return_value=_metadata_success(schema)),
            patch.object(mgr, "install_library_dependencies_request", return_value=_INSTALL_STOP) as mock_install,
            patch.object(
                mgr,
                "download_library_request",
                new_callable=AsyncMock,
                return_value=DownloadLibraryResultFailure(result_details="Clone failed"),
            ),
            patch.object(mgr, "_library_file_path_to_info", {"/mock.json": lib_info}),
        ):
            await mgr._progress_library_through_lifecycle(
                library_info=lib_info,
                file_path="/mock.json",
                request=RegisterLibraryFromFileRequest(file_path="/mock.json"),
            )

        # install was reached (we did NOT return early like required deps do)
        mock_install.assert_called()
        # no dependency problem recorded for an optional dep
        dep_problems = [p for p in lib_info.problems if isinstance(p, LibraryDependencyProblem)]
        assert len(dep_problems) == 0

    @pytest.mark.asyncio
    async def test_registration_failure_after_download_marks_library_unusable(self, engine: Engine) -> None:
        """When download_library_request returns failure, the dependent library is marked UNUSABLE and install is not reached."""
        mgr = engine.library_manager
        lib_info = _make_lib_info()
        schema = _make_schema_mock(["griptape-ai/nodes-dep@v1.0.0"])

        with (
            patch.object(mgr, "load_library_metadata_from_file_request", return_value=_metadata_success(schema)),
            patch.object(mgr, "install_library_dependencies_request") as mock_install,
            patch.object(
                mgr,
                "download_library_request",
                new_callable=AsyncMock,
                return_value=DownloadLibraryResultFailure(
                    result_details="downloaded but failed to register: schema error"
                ),
            ),
            patch.object(mgr, "_library_file_path_to_info", {"/mock.json": lib_info}),
        ):
            result = await mgr._progress_library_through_lifecycle(
                library_info=lib_info,
                file_path="/mock.json",
                request=RegisterLibraryFromFileRequest(file_path="/mock.json"),
            )

        assert isinstance(result, RegisterLibraryFromFileResultFailure)
        mock_install.assert_not_called()
        assert lib_info.fitness == LibraryManager.LibraryFitness.UNUSABLE
        dep_problems = [p for p in lib_info.problems if isinstance(p, LibraryDependencyProblem)]
        assert len(dep_problems) == 1

    @pytest.mark.asyncio
    async def test_failed_dep_already_in_tracker_triggers_download(self, engine: Engine) -> None:
        """A dep in _library_file_path_to_info but in FAILURE state is not treated as satisfied — download must still be attempted."""
        mgr = engine.library_manager
        lib_info = _make_lib_info()
        schema = _make_schema_mock(["griptape-ai/griptape-nodes-library-opencolorio@v1.2.0"])

        failed_dep_info = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.FAILURE,
            library_path="/workspace/libraries/griptape-nodes-library-opencolorio/griptape_nodes_library.json",
            is_sandbox=False,
            library_name="griptape-nodes-library-opencolorio",
            library_version="1.2.0",
            fitness=LibraryManager.LibraryFitness.UNUSABLE,
            problems=[],
        )
        existing_paths = {
            "/workspace/libraries/griptape-nodes-library-opencolorio/griptape_nodes_library.json": failed_dep_info,
            "/mock.json": lib_info,
        }

        with (
            patch.object(mgr, "load_library_metadata_from_file_request", return_value=_metadata_success(schema)),
            patch.object(mgr, "install_library_dependencies_request", return_value=_INSTALL_STOP),
            patch.object(
                mgr,
                "download_library_request",
                new_callable=AsyncMock,
                return_value=DownloadLibraryResultSuccess(
                    library_name="griptape-nodes-library-opencolorio",
                    library_path="/workspace/libraries/griptape-nodes-library-opencolorio/griptape_nodes_library.json",
                    result_details="Downloaded",
                ),
            ) as mock_download,
            patch.object(mgr, "_library_file_path_to_info", existing_paths),
        ):
            await mgr._progress_library_through_lifecycle(
                library_info=lib_info,
                file_path="/mock.json",
                request=RegisterLibraryFromFileRequest(file_path="/mock.json"),
            )

        mock_download.assert_called_once()

    @pytest.mark.asyncio
    async def test_dep_recognized_by_library_name_skips_download(self, engine: Engine) -> None:
        """A dep whose library_name matches repo name skips download even when its path has no matching component."""
        mgr = engine.library_manager
        lib_info = _make_lib_info()
        schema = _make_schema_mock(["griptape-ai/griptape-nodes-library-opencolorio@v1.2.0"])

        # Path parts do not contain 'griptape-nodes-library-opencolorio' — only library_name does.
        custom_path_dep_info = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
            library_path="/custom/monorepo/subdir/griptape_nodes_library.json",
            is_sandbox=False,
            library_name="griptape-nodes-library-opencolorio",
            library_version="1.2.0",
            fitness=LibraryManager.LibraryFitness.GOOD,
            problems=[],
        )
        existing_paths = {
            "/custom/monorepo/subdir/griptape_nodes_library.json": custom_path_dep_info,
            "/mock.json": lib_info,
        }

        with (
            patch.object(mgr, "load_library_metadata_from_file_request", return_value=_metadata_success(schema)),
            patch.object(mgr, "install_library_dependencies_request", return_value=_INSTALL_STOP),
            patch.object(mgr, "download_library_request") as mock_download,
            patch.object(mgr, "_library_file_path_to_info", existing_paths),
        ):
            await mgr._progress_library_through_lifecycle(
                library_info=lib_info,
                file_path="/mock.json",
                request=RegisterLibraryFromFileRequest(file_path="/mock.json"),
            )

        mock_download.assert_not_called()


def _make_lib_info_for_resolve(name: str, version: str = "1.0.0") -> LibraryManager.LibraryInfo:
    return LibraryManager.LibraryInfo(
        lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
        library_path=f"/workspace/libraries/{name}/griptape_nodes_library.json",
        is_sandbox=False,
        library_name=name,
        library_version=version,
        fitness=LibraryManager.LibraryFitness.GOOD,
        problems=[],
    )


def _make_lib_registry_mock(
    library_dependencies: list[LibraryDependencyDeclaration] | None,
) -> MagicMock:
    """Return a mock object as returned by LibraryRegistry.get_library(name)."""
    lib_mock = MagicMock()
    lib_mock.get_library_data.return_value.metadata.declarations = library_dependencies or []
    return lib_mock


class TestResolveTransitiveLibraryDeps:
    """Tests for LibraryManager.resolve_transitive_library_deps()."""

    def test_no_deps_returns_initial(self, engine: Engine) -> None:
        """A library with no library_dependencies returns just the initial set."""
        mgr = engine.library_manager
        lib_a = _make_lib_registry_mock(library_dependencies=None)

        with patch(
            "griptape_nodes.node_library.library_registry.LibraryRegistry.get_library",
            side_effect=lambda name: lib_a if name == "lib-a" else (_ for _ in ()).throw(KeyError(name)),
        ):
            result = mgr.resolve_transitive_library_deps([LibraryNameAndVersion("lib-a", "1.0.0")])

        assert [r.library_name for r in result] == ["lib-a"]

    def test_direct_dep_added(self, engine: Engine) -> None:
        """Library A declaring a library_dependency on Library B includes B in the result."""
        mgr = engine.library_manager
        dep_b = LibraryDependencyDeclaration(url="griptape-ai/lib-b@v1.0.0", required=True)
        lib_a = _make_lib_registry_mock(library_dependencies=[dep_b])
        lib_b = _make_lib_registry_mock(library_dependencies=None)
        info_b = _make_lib_info_for_resolve("lib-b")

        with (
            patch(
                "griptape_nodes.node_library.library_registry.LibraryRegistry.get_library",
                side_effect=lambda name: {"lib-a": lib_a, "lib-b": lib_b}[name],
            ),
            patch.object(
                mgr, "get_library_info_by_library_name", side_effect=lambda n: info_b if n == "lib-b" else None
            ),
        ):
            result = mgr.resolve_transitive_library_deps([LibraryNameAndVersion("lib-a", "1.0.0")])

        names = {r.library_name for r in result}
        assert "lib-a" in names
        assert "lib-b" in names

    def test_transitive_dep_added(self, engine: Engine) -> None:
        """A→B→C chain results in all three libraries being included."""
        mgr = engine.library_manager
        dep_b = LibraryDependencyDeclaration(url="griptape-ai/lib-b@v1.0.0", required=True)
        dep_c = LibraryDependencyDeclaration(url="griptape-ai/lib-c@v1.0.0", required=True)
        lib_a = _make_lib_registry_mock(library_dependencies=[dep_b])
        lib_b = _make_lib_registry_mock(library_dependencies=[dep_c])
        lib_c = _make_lib_registry_mock(library_dependencies=None)
        info_b = _make_lib_info_for_resolve("lib-b")
        info_c = _make_lib_info_for_resolve("lib-c")

        with (
            patch(
                "griptape_nodes.node_library.library_registry.LibraryRegistry.get_library",
                side_effect=lambda name: {"lib-a": lib_a, "lib-b": lib_b, "lib-c": lib_c}[name],
            ),
            patch.object(mgr, "get_library_info_by_library_name", side_effect={"lib-b": info_b, "lib-c": info_c}.get),
        ):
            result = mgr.resolve_transitive_library_deps([LibraryNameAndVersion("lib-a", "1.0.0")])

        assert {r.library_name for r in result} == {"lib-a", "lib-b", "lib-c"}

    def test_cycle_does_not_loop(self, engine: Engine) -> None:
        """A→B→A cycle terminates and includes both libraries exactly once."""
        mgr = engine.library_manager
        dep_b = LibraryDependencyDeclaration(url="griptape-ai/lib-b@v1.0.0", required=True)
        dep_a = LibraryDependencyDeclaration(url="griptape-ai/lib-a@v1.0.0", required=True)
        lib_a = _make_lib_registry_mock(library_dependencies=[dep_b])
        lib_b = _make_lib_registry_mock(library_dependencies=[dep_a])
        info_a = _make_lib_info_for_resolve("lib-a")
        info_b = _make_lib_info_for_resolve("lib-b")

        with (
            patch(
                "griptape_nodes.node_library.library_registry.LibraryRegistry.get_library",
                side_effect=lambda name: {"lib-a": lib_a, "lib-b": lib_b}[name],
            ),
            patch.object(mgr, "get_library_info_by_library_name", side_effect={"lib-a": info_a, "lib-b": info_b}.get),
        ):
            result = mgr.resolve_transitive_library_deps([LibraryNameAndVersion("lib-a", "1.0.0")])

        assert {r.library_name for r in result} == {"lib-a", "lib-b"}

    def test_unregistered_dep_skipped(self, engine: Engine) -> None:
        """A dep that has no LibraryInfo is skipped without raising."""
        mgr = engine.library_manager
        dep_missing = LibraryDependencyDeclaration(url="griptape-ai/lib-missing@v1.0.0", required=True)
        lib_a = _make_lib_registry_mock(library_dependencies=[dep_missing])

        with (
            patch(
                "griptape_nodes.node_library.library_registry.LibraryRegistry.get_library",
                return_value=lib_a,
            ),
            patch.object(mgr, "get_library_info_by_library_name", return_value=None),
        ):
            result = mgr.resolve_transitive_library_deps([LibraryNameAndVersion("lib-a", "1.0.0")])

        assert [r.library_name for r in result] == ["lib-a"]


class TestDownloadLibraryRequestAutoRegister:
    """Regression guard for silent-registration-failure bug (collindutter, PR #4752).

    Old code only logged a warning when auto-registration failed and returned
    DownloadLibraryResultSuccess anyway. The fix at lines 5139-5143 of library_manager.py
    propagates registration failure as DownloadLibraryResultFailure.
    """

    @pytest.mark.asyncio
    async def test_auto_register_failure_returns_download_failure(self, engine: Engine) -> None:
        """When RegisterLibraryFromFileRequest fails, download_library_request must return DownloadLibraryResultFailure.

        Regression guard: old code only logged a warning on registration failure and fell through to
        DownloadLibraryResultSuccess.
        """
        import json as _json
        import tempfile

        mgr = engine.library_manager
        tracked: dict = {}

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_json_path = f"{tmpdir}/fake-library/griptape_nodes_library.json"
            fake_json_content = _json.dumps({"name": "fake-library"})

            mock_path_instance = AsyncMock()
            mock_path_instance.mkdir = AsyncMock(return_value=None)
            # exists() → True forces skip_clone path, avoiding actual git clone
            mock_path_instance.exists = AsyncMock(return_value=True)
            mock_path_instance.read_text = AsyncMock(return_value=fake_json_content)

            with (
                patch(
                    "griptape_nodes.retained_mode.managers.library_manager.anyio.Path",
                    return_value=mock_path_instance,
                ),
                patch(
                    "griptape_nodes.retained_mode.managers.library_manager.find_file_in_directory",
                    return_value=fake_json_path,
                ),
                patch.object(
                    engine,
                    "ahandle_request",
                    new_callable=AsyncMock,
                    return_value=RegisterLibraryFromFileResultFailure(result_details="schema validation error"),
                ),
                patch.object(mgr, "_library_file_path_to_info", tracked),
            ):
                result = await mgr.download_library_request(
                    DownloadLibraryRequest(
                        git_url="https://github.com/griptape-ai/fake-library.git",
                        download_directory=tmpdir,
                        auto_register=True,
                        fail_on_exists=False,
                    )
                )

        assert isinstance(result, DownloadLibraryResultFailure)
        assert "downloaded but failed to register" in str(result.result_details)
        assert "fake-library" in str(result.result_details)


class TestWorkerDelegatedAdvancedLibrarySkip:
    """The orchestrator must not import a worker-delegated library's advanced module.

    The advanced module executes in the engine process and its third-party imports
    resolve against the library venv -- which the orchestrator deliberately never
    creates for worker-delegated libraries (the worker installs its own deps). Before
    the skip, any manifest dependency imported by the advanced module raised
    ModuleNotFoundError on the orchestrator and hard-failed the registration.
    """

    def _make_schema(self, *, advanced_library_path: str | None) -> MagicMock:
        schema = MagicMock()
        schema.name = "test_lib"
        schema.metadata.library_version = "1.0.0"
        schema.metadata.declarations = []
        schema.advanced_library_path = advanced_library_path
        schema.settings = None
        return schema

    def _make_lib_info(
        self, *, state: LibraryManager.LibraryLifecycleState, requires_worker: bool
    ) -> LibraryManager.LibraryInfo:
        return LibraryManager.LibraryInfo(
            lifecycle_state=state,
            library_path="/mock.json",
            is_sandbox=False,
            library_name="test_lib",
            library_version="1.0.0",
            fitness=LibraryManager.LibraryFitness.GOOD,
            requires_worker=requires_worker,
        )

    @pytest.mark.asyncio
    async def test_orchestrator_skips_advanced_module_for_worker_delegated_library(self, engine: Engine) -> None:
        mgr = engine.library_manager
        lib_info = self._make_lib_info(
            state=LibraryManager.LibraryLifecycleState.WORKER_DELEGATED, requires_worker=True
        )
        schema = self._make_schema(advanced_library_path="lib_advanced.py")

        with (
            patch.object(mgr, "load_library_metadata_from_file_request", return_value=_metadata_success(schema)),
            patch.object(mgr, "_add_library_paths_to_sys_path", new=AsyncMock()),
            patch.object(mgr, "_load_advanced_library_module") as mock_advanced,
            patch.object(mgr, "_library_file_path_to_info", {"/mock.json": lib_info}),
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.LibraryRegistry.generate_new_library",
                return_value=MagicMock(),
            ) as mock_generate,
        ):
            result = await mgr._progress_library_through_lifecycle(
                library_info=lib_info,
                file_path="/mock.json",
                request=RegisterLibraryFromFileRequest(file_path="/mock.json"),
            )

        assert result is None
        mock_advanced.assert_not_called()
        # The library still registers -- with no advanced instance -- so the editor and
        # workflow loading see it while the worker loads the real module in its process.
        assert mock_generate.call_args.kwargs["advanced_library"] is None
        assert lib_info.lifecycle_state is LibraryManager.LibraryLifecycleState.WORKER_PENDING

    @pytest.mark.asyncio
    async def test_in_process_library_still_loads_the_advanced_module(self, engine: Engine) -> None:
        """Pins the non-worker path: the skip must key on worker delegation, not fire always."""
        mgr = engine.library_manager
        lib_info = self._make_lib_info(
            state=LibraryManager.LibraryLifecycleState.DEPENDENCIES_INSTALLED, requires_worker=False
        )
        schema = self._make_schema(advanced_library_path="lib_advanced.py")
        advanced_instance = MagicMock()

        with (
            patch.object(mgr, "load_library_metadata_from_file_request", return_value=_metadata_success(schema)),
            patch.object(mgr, "_add_library_paths_to_sys_path", new=AsyncMock()),
            patch.object(mgr, "_load_advanced_library_module", return_value=advanced_instance) as mock_advanced,
            patch.object(mgr, "_attempt_load_nodes_from_library"),
            patch.object(mgr, "_library_file_path_to_info", {"/mock.json": lib_info}),
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.LibraryRegistry.generate_new_library",
                return_value=MagicMock(),
            ) as mock_generate,
        ):
            result = await mgr._progress_library_through_lifecycle(
                library_info=lib_info,
                file_path="/mock.json",
                request=RegisterLibraryFromFileRequest(file_path="/mock.json"),
            )

        assert result is None
        mock_advanced.assert_called_once()
        assert mock_generate.call_args.kwargs["advanced_library"] is advanced_instance


class TestRequiresWorkerResolvedOnFilePathRegistration:
    """A by-file-path registration must resolve requires_worker for itself.

    This path enters METADATA_LOADED directly, so the DISCOVERED-phase writer that normally
    resolves requires_worker never runs. Without resolving it here, a library whose manifest
    suggests worker mode loads in-process when added to a running engine from the editor --
    and its advanced module then imports manifest dependencies against a venv the
    orchestrator deliberately never created (the pygit2 breakage).
    """

    def _worker_mode_schema(self) -> MagicMock:
        schema = MagicMock()
        schema.name = "worker_mode_lib"
        schema.metadata.library_version = "1.0.0"
        schema.metadata.declarations = [SuggestedWorkerMode(mode=WorkerMode.WORKER)]
        return schema

    def _request(self) -> RegisterLibraryFromFileRequest:
        return RegisterLibraryFromFileRequest(file_path="/mock.json", perform_discovery_if_not_found=False)

    @pytest.mark.asyncio
    async def test_an_existing_library_info_is_updated_in_place(self, engine: Engine) -> None:
        """Discovered-but-unnamed LibraryInfo: the stale requires_worker must be overwritten.

        `_library_file_path_to_info` can already hold an entry from discovery that never got a
        library_name (so it defaulted requires_worker to False). Updating every other field
        while leaving that one stale is how a worker library ends up loading in-process.
        """
        mgr = engine.library_manager
        lib_info = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.DISCOVERED,
            library_path="/mock.json",
            is_sandbox=False,
            library_name=None,
            fitness=LibraryManager.LibraryFitness.NOT_EVALUATED,
            requires_worker=False,
        )

        with (
            patch.object(
                mgr,
                "load_library_metadata_from_file_request",
                return_value=_metadata_success(self._worker_mode_schema()),
            ),
            patch.object(mgr, "_library_file_path_to_info", {"/mock.json": lib_info}),
        ):
            result = await mgr._establish_register_library_prerequisites(self._request())

        assert isinstance(result, LibraryManager.RegisterLibraryPrerequisites)
        assert result.library_info is lib_info
        assert lib_info.requires_worker is True
        assert lib_info.library_name == "worker_mode_lib"
        assert lib_info.lifecycle_state is LibraryManager.LibraryLifecycleState.METADATA_LOADED

    @pytest.mark.asyncio
    async def test_a_new_library_info_is_created_with_it(self, engine: Engine) -> None:
        """Nothing discovered yet -- the freshly built LibraryInfo carries the resolved value."""
        mgr = engine.library_manager

        with (
            patch.object(
                mgr,
                "load_library_metadata_from_file_request",
                return_value=_metadata_success(self._worker_mode_schema()),
            ),
            patch.object(mgr, "_library_file_path_to_info", {}),
        ):
            result = await mgr._establish_register_library_prerequisites(self._request())

        assert isinstance(result, LibraryManager.RegisterLibraryPrerequisites)
        assert result.library_info.requires_worker is True
        assert result.library_info.lifecycle_state is LibraryManager.LibraryLifecycleState.METADATA_LOADED

    @pytest.mark.asyncio
    async def test_a_library_with_no_worker_declaration_stays_in_process(self, engine: Engine) -> None:
        """The resolution must read the manifest, not default to worker mode for everyone."""
        mgr = engine.library_manager
        schema = self._worker_mode_schema()
        schema.metadata.declarations = []

        with (
            patch.object(mgr, "load_library_metadata_from_file_request", return_value=_metadata_success(schema)),
            patch.object(mgr, "_library_file_path_to_info", {}),
        ):
            result = await mgr._establish_register_library_prerequisites(self._request())

        assert isinstance(result, LibraryManager.RegisterLibraryPrerequisites)
        assert result.library_info.requires_worker is False


class TestPipInstallFailureIsRecordedOnTheLibrary:
    """A failed dependency install must leave an account of itself on the LibraryInfo.

    Until this was wired, a pip failure recorded nothing: the resolver's complaint went to a log
    line and the library itself reported no problem at all. Anything that later asked the
    library what was wrong with it -- the settings panel, or a worker explaining why it cannot
    run a node -- had nothing to report.
    """

    @pytest.mark.asyncio
    async def test_a_failed_install_records_a_dependency_installation_problem(self, engine: Engine) -> None:
        mgr = engine.library_manager
        lib_info = _make_lib_info()
        schema = _make_schema_mock([])

        failure = InstallLibraryDependenciesResultFailure(
            result_details="No solution found when resolving dependencies: nonexistent-package"
        )
        with (
            patch.object(mgr, "load_library_metadata_from_file_request", return_value=_metadata_success(schema)),
            patch.object(mgr, "install_library_dependencies_request", return_value=failure),
            patch.object(mgr, "_library_file_path_to_info", {"/mock.json": lib_info}),
        ):
            await mgr._progress_library_through_lifecycle(
                library_info=lib_info,
                file_path="/mock.json",
                request=RegisterLibraryFromFileRequest(file_path="/mock.json"),
            )

        install_problems = [p for p in lib_info.problems if isinstance(p, DependencyInstallationFailedProblem)]
        assert len(install_problems) == 1
        assert "nonexistent-package" in install_problems[0].error_details
        # Readable by the collator the settings panel and the worker both go through.
        collated = mgr.collate_problems_for_lib_info(lib_info)
        assert collated is not None
        assert "nonexistent-package" in collated

    @pytest.mark.asyncio
    async def test_a_failed_install_is_not_reported_as_a_missing_library_dependency(self, engine: Engine) -> None:
        """The two are different failures and must not be conflated.

        LibraryDependencyProblem means another griptape LIBRARY could not be fetched. Reusing it
        for a pip failure would misreport the cause, and would silently change what the existing
        dependency tests observe, since they filter problems by exactly that type.
        """
        mgr = engine.library_manager
        lib_info = _make_lib_info()
        schema = _make_schema_mock([])

        with (
            patch.object(mgr, "load_library_metadata_from_file_request", return_value=_metadata_success(schema)),
            patch.object(mgr, "install_library_dependencies_request", return_value=_INSTALL_STOP),
            patch.object(mgr, "_library_file_path_to_info", {"/mock.json": lib_info}),
        ):
            await mgr._progress_library_through_lifecycle(
                library_info=lib_info,
                file_path="/mock.json",
                request=RegisterLibraryFromFileRequest(file_path="/mock.json"),
            )

        assert not [p for p in lib_info.problems if isinstance(p, LibraryDependencyProblem)]
