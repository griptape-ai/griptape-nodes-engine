"""Tests for WorkflowPackager transitive library dependency resolution."""

from unittest.mock import MagicMock, patch

from griptape_nodes.node_library.library_registry import LibraryNameAndVersion
from griptape_nodes.retained_mode.publishing.workflow_packager import WorkflowPackager


def _make_library_data_mock(
    pip_dependencies: list[str] | None = None,
    pip_install_flags: list[str] | None = None,
) -> MagicMock:
    """Return a mock library.get_library_data() with the given pip dependency fields."""
    deps_mock = MagicMock()
    deps_mock.pip_dependencies = pip_dependencies
    deps_mock.pip_install_flags = pip_install_flags

    metadata_mock = MagicMock()
    metadata_mock.dependencies = deps_mock

    schema_mock = MagicMock()
    schema_mock.metadata = metadata_mock

    library_mock = MagicMock()
    library_mock.get_library_data.return_value = schema_mock

    return library_mock


def _make_workflow_mock(library_names: list[str]) -> MagicMock:
    workflow = MagicMock()
    workflow.metadata.node_libraries_referenced = [
        LibraryNameAndVersion(library_name=name, library_version="1.0.0") for name in library_names
    ]
    return workflow


def _make_lib_manager_mock(resolved: list[LibraryNameAndVersion]) -> MagicMock:
    """Return a LibraryManager mock whose resolve_transitive_library_deps returns `resolved`."""
    return MagicMock(resolve_transitive_library_deps=lambda _initial: resolved)


class TestResolveAllLibraryDeps:
    """_resolve_all_library_deps delegates to LibraryManager.resolve_transitive_library_deps."""

    def test_delegates_to_library_manager(self) -> None:
        """_resolve_all_library_deps returns whatever resolve_transitive_library_deps returns."""
        packager = WorkflowPackager("test_workflow")
        initial = [LibraryNameAndVersion("lib-a", "1.0.0")]
        expected = [LibraryNameAndVersion("lib-a", "1.0.0"), LibraryNameAndVersion("lib-b", "1.0.0")]

        with patch(
            "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.LibraryManager",
            return_value=_make_lib_manager_mock(expected),
        ):
            result = packager._resolve_all_library_deps(initial)

        assert result == expected

    def test_passes_initial_list_through(self) -> None:
        """The initial library list is forwarded unchanged to resolve_transitive_library_deps."""
        packager = WorkflowPackager("test_workflow")
        initial = [LibraryNameAndVersion("lib-a", "1.0.0")]
        captured: list[list[LibraryNameAndVersion]] = []

        def capture_and_return(libs: list[LibraryNameAndVersion]) -> list[LibraryNameAndVersion]:
            captured.append(libs)
            return libs

        with patch(
            "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.LibraryManager",
            return_value=MagicMock(resolve_transitive_library_deps=capture_and_return),
        ):
            packager._resolve_all_library_deps(initial)

        assert captured[0] == initial


class TestCollectDependenciesTransitive:
    """collect_dependencies includes pip deps from transitive library dependencies."""

    def test_includes_pip_deps_from_transitive_library(self) -> None:
        """Workflow uses Library A; A depends on Library B; B's pip deps appear in result."""
        packager = WorkflowPackager("test_workflow")
        workflow = _make_workflow_mock(["lib-a"])

        lib_a = _make_library_data_mock(pip_dependencies=["requests>=2.0"])
        lib_b = _make_library_data_mock(pip_dependencies=["numpy>=1.0"])
        resolved = [LibraryNameAndVersion("lib-a", "1.0.0"), LibraryNameAndVersion("lib-b", "1.0.0")]

        with (
            patch(
                "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.LibraryManager",
                return_value=_make_lib_manager_mock(resolved),
            ),
            patch(
                "griptape_nodes.retained_mode.publishing.workflow_packager.LibraryRegistry.get_library",
                side_effect=lambda name: {"lib-a": lib_a, "lib-b": lib_b}[name],
            ),
            patch.object(packager, "get_engine_version", return_value="0.0.0"),
            patch.object(packager, "get_install_source", return_value=("pypi", None)),
        ):
            result = packager.collect_dependencies(workflow)

        assert "numpy>=1.0" in result
        assert "requests>=2.0" in result


class TestCollectPipInstallFlagsTransitive:
    """collect_pip_install_flags includes flags from transitive library dependencies."""

    def test_includes_flags_from_transitive_library(self) -> None:
        """Workflow uses Library A; A depends on Library B; B's pip flags appear in result."""
        packager = WorkflowPackager("test_workflow")
        workflow = _make_workflow_mock(["lib-a"])

        lib_a = _make_library_data_mock(pip_install_flags=["--extra-index-url=https://a.example.com"])
        lib_b = _make_library_data_mock(pip_install_flags=["--extra-index-url=https://b.example.com"])
        resolved = [LibraryNameAndVersion("lib-a", "1.0.0"), LibraryNameAndVersion("lib-b", "1.0.0")]

        with (
            patch(
                "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.LibraryManager",
                return_value=_make_lib_manager_mock(resolved),
            ),
            patch(
                "griptape_nodes.retained_mode.publishing.workflow_packager.LibraryRegistry.get_library",
                side_effect=lambda name: {"lib-a": lib_a, "lib-b": lib_b}[name],
            ),
        ):
            result = packager.collect_pip_install_flags(workflow)

        assert "--extra-index-url=https://a.example.com" in result
        assert "--extra-index-url=https://b.example.com" in result
