"""Tests for WorkflowPackager: library dependency resolution and clean-rebuild publishing."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from dotenv import dotenv_values

from griptape_nodes.node_library.library_registry import LibraryNameAndVersion
from griptape_nodes.retained_mode.events.os_events import (
    DeleteFileRequest,
    DeleteFileResultSuccess,
    FileIOFailureReason,
    MakeDirectoryRequest,
    RenameFileRequest,
    RenameFileResultFailure,
    WriteFileResultSuccess,
)
from griptape_nodes.retained_mode.events.project_events import (
    GetPathForMacroRequest,
    GetPathForMacroResultSuccess,
)
from griptape_nodes.retained_mode.events.secrets_events import (
    GetAllSecretValuesRequest,
    GetAllSecretValuesResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.publishing.workflow_packager import (
    DOWNLOAD_MODELS_SCRIPT_NAME,
    ResolvedFileReference,
    WorkflowPackager,
)


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


def _macro_resolution_patch(resolved_path: Path, absolute_path: Path):  # noqa: ANN202
    """Patch handle_request so a macro reference resolves to the given pair of paths.

    Mirrors ProjectManager.on_get_path_for_macro_request, whose ``resolved_path`` is the macro
    string after substitution: absolute when a directory macro is absolute-rooted, relative
    otherwise. Any other request returns a MagicMock, so the current-project lookup fails its
    isinstance check and leaves the project anchor out.
    """

    def handle_request(request: object) -> object:
        if isinstance(request, GetPathForMacroRequest):
            return GetPathForMacroResultSuccess(
                resolved_path=resolved_path,
                absolute_path=absolute_path,
                result_details="resolved",
            )
        return MagicMock()

    return patch(
        "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.handle_request",
        side_effect=handle_request,
    )


def _workspace_patch(workspace_dir: Path):  # noqa: ANN202
    """Patch the config manager so the workspace anchor is ``workspace_dir``."""
    return patch(
        "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.ConfigManager",
        return_value=MagicMock(workspace_path=workspace_dir),
    )


class TestCopyStaticFilesBundleDestination:
    """copy_static_files places a dependency where the published bundle looks for it."""

    def test_bundles_file_referenced_by_absolute_macro_path(self, tmp_path: Path) -> None:
        """A macro that substitutes to an absolute path still lands inside the bundle.

        The v1 default anchors `{inputs}` on `{workflow_dir}`, so `{inputs}/image.jpg`
        substitutes to an absolute string. Joining that onto the destination discards the
        destination, which silently dropped every macro-referenced dependency.
        """
        packager = WorkflowPackager("test_workflow")
        workspace = tmp_path / "ws"
        source = workspace / "inputs" / "image.jpg"
        source.parent.mkdir(parents=True)
        source.write_text("data")
        destination = tmp_path / "bundle"

        with (
            _macro_resolution_patch(resolved_path=source, absolute_path=source),
            _workspace_patch(workspace),
            patch.object(packager, "copy_file") as mock_copy_file,
            patch.object(packager, "copy_tree") as mock_copy_tree,
        ):
            packager.copy_static_files([("node", "{inputs}/image.jpg")], destination, workspace)

        mock_copy_file.assert_called_once_with(source, destination / "inputs" / "image.jpg")
        mock_copy_tree.assert_not_called()

    def test_bundles_file_relative_to_the_workflow_not_the_workspace(self, tmp_path: Path) -> None:
        """A workflow in a subdirectory resolves against its own directory, not the workspace.

        The bundle copies the workflow file to its root, so `{inputs}` resolves there at run
        time. Anchoring on the workspace instead would bundle the file under `shots/inputs/`,
        where nothing looks for it.
        """
        packager = WorkflowPackager("test_workflow")
        workspace = tmp_path / "ws"
        workflow_dir = workspace / "shots"
        source = workflow_dir / "inputs" / "image.jpg"
        source.parent.mkdir(parents=True)
        source.write_text("data")
        destination = tmp_path / "bundle"

        with (
            _macro_resolution_patch(resolved_path=source, absolute_path=source),
            _workspace_patch(workspace),
            patch.object(packager, "copy_file") as mock_copy_file,
            patch.object(packager, "copy_tree") as mock_copy_tree,
        ):
            packager.copy_static_files([("node", "{inputs}/image.jpg")], destination, workflow_dir)

        mock_copy_file.assert_called_once_with(source, destination / "inputs" / "image.jpg")
        mock_copy_tree.assert_not_called()

    def test_bundles_file_referenced_by_relative_path(self, tmp_path: Path) -> None:
        """A reference that substitutes to a relative path keeps bundling as it did before."""
        packager = WorkflowPackager("test_workflow")
        workspace = tmp_path / "ws"
        source = workspace / "inputs" / "image.jpg"
        source.parent.mkdir(parents=True)
        source.write_text("data")
        destination = tmp_path / "bundle"

        with (
            _macro_resolution_patch(resolved_path=Path("inputs/image.jpg"), absolute_path=source),
            _workspace_patch(workspace),
            patch.object(packager, "copy_file") as mock_copy_file,
            patch.object(packager, "copy_tree") as mock_copy_tree,
        ):
            packager.copy_static_files([("node", "inputs/image.jpg")], destination, workspace)

        mock_copy_file.assert_called_once_with(source, destination / "inputs" / "image.jpg")
        mock_copy_tree.assert_not_called()

    def test_reports_a_file_that_resolves_outside_every_anchor(self, tmp_path: Path) -> None:
        """A file on an external volume has no place in the bundle, and the publish says so."""
        packager = WorkflowPackager("test_workflow")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        source = tmp_path / "external" / "image.jpg"
        source.parent.mkdir(parents=True)
        source.write_text("data")
        destination = tmp_path / "bundle"

        with (
            _macro_resolution_patch(resolved_path=source, absolute_path=source),
            _workspace_patch(workspace),
            patch.object(packager, "copy_file") as mock_copy_file,
            patch.object(packager, "copy_tree") as mock_copy_tree,
            patch.object(packager, "emit_progress") as mock_emit_progress,
        ):
            packager.copy_static_files([("node", "{external}/image.jpg")], destination, workspace)

        mock_copy_file.assert_not_called()
        mock_copy_tree.assert_not_called()
        assert "{external}/image.jpg" in mock_emit_progress.call_args.args[1]


class TestCopyStaticFiles:
    """copy_static_files handles the case where the destination resolves back onto the source."""

    def test_skips_copy_when_source_and_dest_are_same_file(self, tmp_path: Path) -> None:
        """A file whose destination resolves to itself is left in place instead of copied."""
        packager = WorkflowPackager("test_workflow")
        source = tmp_path / "inputs" / "images" / "img.jpg"
        source.parent.mkdir(parents=True)
        source.write_text("data")
        resolved = ResolvedFileReference(absolute_path=source, bundle_relative_path=Path("inputs/images/img.jpg"))

        # destination is the project root itself, so dest == source.
        with (
            patch.object(packager, "_resolve_file_reference", return_value=resolved),
            patch(
                "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.handle_request",
                return_value=MagicMock(),
            ),
            _workspace_patch(tmp_path),
            patch.object(packager, "copy_file") as mock_copy_file,
            patch.object(packager, "copy_tree") as mock_copy_tree,
        ):
            packager.copy_static_files([("node", "img.jpg")], tmp_path, tmp_path)

        mock_copy_file.assert_not_called()
        mock_copy_tree.assert_not_called()

    def test_copies_when_source_and_dest_differ(self, tmp_path: Path) -> None:
        """A file whose destination differs from the source is copied."""
        packager = WorkflowPackager("test_workflow")
        source = tmp_path / "inputs" / "images" / "img.jpg"
        source.parent.mkdir(parents=True)
        source.write_text("data")
        relative = Path("inputs/images/img.jpg")
        resolved = ResolvedFileReference(absolute_path=source, bundle_relative_path=relative)
        destination = tmp_path / "bundle"

        with (
            patch.object(packager, "_resolve_file_reference", return_value=resolved),
            patch(
                "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.handle_request",
                return_value=MagicMock(),
            ),
            _workspace_patch(tmp_path),
            patch.object(packager, "copy_file") as mock_copy_file,
            patch.object(packager, "copy_tree") as mock_copy_tree,
        ):
            packager.copy_static_files([("node", "img.jpg")], destination, tmp_path)

        mock_copy_file.assert_called_once_with(source, destination / relative)
        mock_copy_tree.assert_not_called()


class TestGetInstallSource:
    """get_install_source pins the full commit SHA so the git ref is fetchable from the remote."""

    def test_vcs_info_returns_full_commit_id(self) -> None:
        """A git install exposes the full 40-char commit SHA, not an abbreviated one."""
        packager = WorkflowPackager("test_workflow")
        full_sha = "d1e0a500e25ced659d30d82f0cae4073523e42a5"
        dist = MagicMock()
        dist.read_text.return_value = json.dumps(
            {"url": "https://github.com/griptape-ai/griptape-nodes.git", "vcs_info": {"commit_id": full_sha}}
        )

        with patch.object(packager, "find_griptape_nodes_distribution", return_value=dist):
            source, commit = packager.get_install_source()

        assert source == "git"
        assert commit == full_sha

    def test_vcs_info_without_commit_falls_back_to_pypi(self) -> None:
        """A git install missing its commit id falls back to pypi instead of an empty ref."""
        packager = WorkflowPackager("test_workflow")
        dist = MagicMock()
        dist.read_text.return_value = json.dumps(
            {"url": "https://github.com/griptape-ai/griptape-nodes.git", "vcs_info": {}}
        )

        with patch.object(packager, "find_griptape_nodes_distribution", return_value=dist):
            source, commit = packager.get_install_source()

        assert source == "pypi"
        assert commit is None

    def test_editable_install_resolves_commit_from_source_checkout(self) -> None:
        """An editable (file://) install resolves the commit from the checkout url, not site-packages."""
        packager = WorkflowPackager("test_workflow")
        full_sha = "a11a1dd14af250387e60127a1ed63841f0950db3"
        url = "file:///Users/dev/griptape-nodes-engine"
        checkout = "/checkout/griptape-nodes-engine"
        dist = MagicMock()
        dist.read_text.return_value = json.dumps({"url": url, "dir_info": {"editable": True}})

        with (
            patch.object(packager, "find_griptape_nodes_distribution", return_value=dist),
            patch(
                "griptape_nodes.retained_mode.publishing.workflow_packager.shutil.which",
                return_value="/usr/bin/git",
            ),
            patch(
                "griptape_nodes.retained_mode.publishing.workflow_packager.url2pathname",
                return_value=checkout,
            ) as mock_url2pathname,
            patch(
                "griptape_nodes.retained_mode.publishing.workflow_packager.subprocess.check_output",
                return_value=(full_sha + "\n").encode(),
            ) as mock_check_output,
        ):
            source, commit = packager.get_install_source()

        assert source == "git"
        assert commit == full_sha
        # The path handed to the filesystem converter comes from the url's path component
        # (the checkout), not from dist.locate_file()/site-packages.
        mock_url2pathname.assert_called_once_with("/Users/dev/griptape-nodes-engine")
        # git resolves the commit in that checkout directory.
        assert mock_check_output.call_args.args[0] == ["/usr/bin/git", "-C", str(Path(checkout)), "rev-parse", "HEAD"]

    def test_editable_install_without_git_repo_falls_back_to_file(self) -> None:
        """A file:// install whose checkout is not a git repo reports 'file' with no commit."""
        packager = WorkflowPackager("test_workflow")
        dist = MagicMock()
        dist.read_text.return_value = json.dumps(
            {"url": "file:///Users/dev/griptape-nodes-engine", "dir_info": {"editable": True}}
        )

        with (
            patch.object(packager, "find_griptape_nodes_distribution", return_value=dist),
            patch(
                "griptape_nodes.retained_mode.publishing.workflow_packager.shutil.which",
                return_value="/usr/bin/git",
            ),
            patch(
                "griptape_nodes.retained_mode.publishing.workflow_packager.subprocess.check_output",
                side_effect=subprocess.CalledProcessError(128, "git"),
            ),
        ):
            source, commit = packager.get_install_source()

        assert source == "file"
        assert commit is None


class TestCollectDependenciesEnginePin:
    """collect_dependencies pins the engine to the full commit SHA for git installs."""

    def test_pins_full_git_sha(self) -> None:
        """The engine dependency uses the full SHA returned by get_install_source verbatim."""
        packager = WorkflowPackager("test_workflow")
        workflow = _make_workflow_mock([])
        full_sha = "d1e0a500e25ced659d30d82f0cae4073523e42a5"

        with (
            patch(
                "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.LibraryManager",
                return_value=_make_lib_manager_mock([]),
            ),
            patch.object(packager, "get_engine_version", return_value="v0.92.0"),
            patch.object(packager, "get_install_source", return_value=("git", full_sha)),
        ):
            result = packager.collect_dependencies(workflow)

        assert f"griptape-nodes-engine @ git+https://github.com/griptape-ai/griptape-nodes.git@{full_sha}" in result

    def test_pins_engine_version_tag_for_pypi(self) -> None:
        """A pypi install pins the released version tag rather than a commit."""
        packager = WorkflowPackager("test_workflow")
        workflow = _make_workflow_mock([])

        with (
            patch(
                "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.LibraryManager",
                return_value=_make_lib_manager_mock([]),
            ),
            patch.object(packager, "get_engine_version", return_value="v0.92.0"),
            patch.object(packager, "get_install_source", return_value=("pypi", None)),
        ):
            result = packager.collect_dependencies(workflow)

        assert "griptape-nodes-engine @ git+https://github.com/griptape-ai/griptape-nodes.git@v0.92.0" in result


def _write_file_via_real_fs(request: MagicMock) -> MagicMock:
    """Handle a WriteFileRequest by actually writing the file, returning a success result."""
    path = Path(request.file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(request.content, encoding="utf-8")
    return MagicMock(spec=WriteFileResultSuccess)


class TestGetMergedEnvMapping:
    """get_merged_env_mapping drops blank-valued entries from both sources."""

    def test_drops_blank_workspace_entries(self, tmp_path: Path) -> None:
        """A blank value in the workspace .env is not carried into the bundle."""
        workspace_env = tmp_path / ".env"
        workspace_env.write_text("GT_CLOUD_API_KEY=\nOTHER_KEY=value\n", encoding="utf-8")

        with patch(
            "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.handle_request",
            return_value=MagicMock(spec=GetAllSecretValuesResultSuccess, values={}),
        ):
            result = WorkflowPackager.get_merged_env_mapping(workspace_env)

        assert "GT_CLOUD_API_KEY" not in result
        assert result["OTHER_KEY"] == "value"

    def test_drops_blank_secrets(self, tmp_path: Path) -> None:
        """A registered-but-empty secret is not carried into the bundle."""
        workspace_env = tmp_path / ".env"

        with patch(
            "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.handle_request",
            return_value=MagicMock(
                spec=GetAllSecretValuesResultSuccess, values={"GT_CLOUD_API_KEY": "", "REAL_KEY": "abc"}
            ),
        ):
            result = WorkflowPackager.get_merged_env_mapping(workspace_env)

        assert "GT_CLOUD_API_KEY" not in result
        assert result["REAL_KEY"] == "abc"

    def test_blank_workspace_entry_does_not_shadow_real_secret(self, tmp_path: Path) -> None:
        """A blank workspace entry lets the real secret value through instead of masking it."""
        workspace_env = tmp_path / ".env"
        workspace_env.write_text("GT_CLOUD_API_KEY=\n", encoding="utf-8")

        with patch(
            "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.handle_request",
            return_value=MagicMock(spec=GetAllSecretValuesResultSuccess, values={"GT_CLOUD_API_KEY": "real-key"}),
        ):
            result = WorkflowPackager.get_merged_env_mapping(workspace_env)

        assert result["GT_CLOUD_API_KEY"] == "real-key"

    def test_raises_when_secret_read_fails(self, tmp_path: Path) -> None:
        """A failed secret read raises rather than yielding a thinner mapping."""
        workspace_env = tmp_path / ".env"

        with (
            patch(
                "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.handle_request",
                return_value=MagicMock(),
            ),
            pytest.raises(TypeError),
        ):
            WorkflowPackager.get_merged_env_mapping(workspace_env)


class TestWriteEnvFile:
    """write_env_file rebuilds the file rather than updating it key-by-key."""

    def test_removes_keys_absent_from_the_mapping(self, tmp_path: Path) -> None:
        """A key left by an earlier publish is gone after a publish that does not include it."""
        env_path = tmp_path / ".env"
        env_path.write_text("GT_CLOUD_API_KEY=''\nSTALE_KEY='old'\n", encoding="utf-8")

        with patch(
            "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.handle_request",
            side_effect=_write_file_via_real_fs,
        ):
            WorkflowPackager.write_env_file(env_path, {"GT_CLOUD_API_KEY": "real-key"})

        assert dotenv_values(env_path) == {"GT_CLOUD_API_KEY": "real-key"}

    def test_values_round_trip_through_dotenv(self, tmp_path: Path) -> None:
        """Values with quotes, spaces, and '#' survive the write unchanged."""
        env_path = tmp_path / ".env"
        mapping = {"WITH_SPACE": "a b", "WITH_QUOTE": "it's", "WITH_HASH": "a#b", "PLAIN": "abc123"}

        with patch(
            "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.handle_request",
            side_effect=_write_file_via_real_fs,
        ):
            WorkflowPackager.write_env_file(env_path, mapping)

        assert dotenv_values(env_path) == mapping

    def test_raises_when_the_write_fails(self, tmp_path: Path) -> None:
        """A failed write raises instead of leaving a partial file unreported."""
        with (
            patch(
                "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.handle_request",
                return_value=MagicMock(),
            ),
            pytest.raises(TypeError),
        ):
            WorkflowPackager.write_env_file(tmp_path / ".env", {"KEY": "value"})


class TestGetProcessEnvSecrets:
    """get_process_env_secrets picks up registered secrets exported into the environment."""

    def test_returns_registered_secret_set_in_the_environment(self) -> None:
        """A registered secret exported in the shell is available to the bundle."""
        secrets_manager = MagicMock(secrets_to_register={"GT_CLOUD_API_KEY": ""})

        with (
            patch(
                "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.SecretsManager",
                return_value=secrets_manager,
            ),
            patch.dict("os.environ", {"GT_CLOUD_API_KEY": "from-shell"}, clear=False),
        ):
            result = WorkflowPackager.get_process_env_secrets()

        assert result == {"GT_CLOUD_API_KEY": "from-shell"}

    def test_skips_unregistered_environment_variables(self) -> None:
        """Only registered secret names are read, so unrelated process state stays out."""
        secrets_manager = MagicMock(secrets_to_register={"GT_CLOUD_API_KEY": ""})

        with (
            patch(
                "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.SecretsManager",
                return_value=secrets_manager,
            ),
            patch.dict("os.environ", {"GT_CLOUD_API_KEY": "from-shell", "UNRELATED_SECRET": "nope"}, clear=False),
        ):
            result = WorkflowPackager.get_process_env_secrets()

        assert result == {"GT_CLOUD_API_KEY": "from-shell"}

    def test_skips_blank_environment_values(self) -> None:
        """An exported-but-empty variable is not treated as a credential."""
        secrets_manager = MagicMock(secrets_to_register={"GT_CLOUD_API_KEY": ""})

        with (
            patch(
                "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.SecretsManager",
                return_value=secrets_manager,
            ),
            patch.dict("os.environ", {"GT_CLOUD_API_KEY": ""}, clear=False),
        ):
            result = WorkflowPackager.get_process_env_secrets()

        assert result == {}


class TestWriteEnv:
    """write_env resolves its sources in the order SecretsManager.get_secret does."""

    def test_exported_value_wins_over_the_workspace_env_file(self, tmp_path: Path) -> None:
        """An exported secret is bundled ahead of a different value on disk.

        ``get_secret`` resolves OS environment variables ahead of both .env files, so
        bundling the file's value would ship a credential the live session does not use.
        """
        packager = WorkflowPackager("test_workflow")
        workspace_env = tmp_path / "workspace" / ".env"
        workspace_env.parent.mkdir()
        workspace_env.write_text("GT_CLOUD_API_KEY='from-file'\n", encoding="utf-8")
        destination = tmp_path / "bundle"
        secrets_manager = MagicMock(workspace_env_path=workspace_env, secrets_to_register={"GT_CLOUD_API_KEY": ""})

        def handle_request(request: MagicMock) -> MagicMock:
            """Serve the secret read from the mock, and write files to the real filesystem."""
            if isinstance(request, GetAllSecretValuesRequest):
                return MagicMock(spec=GetAllSecretValuesResultSuccess, values={})
            return _write_file_via_real_fs(request)

        with (
            patch(
                "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.SecretsManager",
                return_value=secrets_manager,
            ),
            patch(
                "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.handle_request",
                side_effect=handle_request,
            ),
            patch.dict("os.environ", {"GT_CLOUD_API_KEY": "from-shell"}, clear=False),
        ):
            packager.write_env(destination)

        assert dotenv_values(destination / ".env")["GT_CLOUD_API_KEY"] == "from-shell"


class TestWriteDownloadModelsScript:
    """A workflow with no HuggingFace models leaves no download script behind."""

    def test_removes_stale_script_when_no_models_are_needed(self, tmp_path: Path) -> None:
        """A script from an earlier publish is deleted, not left to run again."""
        packager = WorkflowPackager("test_workflow")
        script_path = tmp_path / DOWNLOAD_MODELS_SCRIPT_NAME
        script_path.write_text("# from an earlier publish\n", encoding="utf-8")

        def delete_it(request: MagicMock) -> MagicMock:
            Path(request.path).unlink()
            return MagicMock(spec=DeleteFileResultSuccess)

        with (
            patch.object(packager, "collect_huggingface_download_commands", return_value=[]),
            patch(
                "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.handle_request",
                side_effect=delete_it,
            ),
        ):
            wrote = packager.write_download_models_script([], tmp_path)

        assert wrote is False
        assert not script_path.exists()

    def test_raises_when_stale_script_cannot_be_removed(self, tmp_path: Path) -> None:
        """A failed removal fails the publish rather than shipping a script that will run."""
        packager = WorkflowPackager("test_workflow")
        (tmp_path / DOWNLOAD_MODELS_SCRIPT_NAME).write_text("# from an earlier publish\n", encoding="utf-8")

        with (
            patch.object(packager, "collect_huggingface_download_commands", return_value=[]),
            patch(
                "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.handle_request",
                return_value=MagicMock(),
            ),
            pytest.raises(TypeError),
        ):
            packager.write_download_models_script([], tmp_path)

    def test_no_delete_attempted_when_no_script_exists(self, tmp_path: Path) -> None:
        """The common case (nothing to clean up) issues no delete request."""
        packager = WorkflowPackager("test_workflow")

        with (
            patch.object(packager, "collect_huggingface_download_commands", return_value=[]),
            patch(
                "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.handle_request"
            ) as mock_handle,
        ):
            wrote = packager.write_download_models_script([], tmp_path)

        assert wrote is False
        mock_handle.assert_not_called()


class TestStagedPublish:
    """staged_publish makes a re-publish a clean rewrite, and a failed publish a no-op."""

    def test_swaps_staging_into_place_on_success(self, tmp_path: Path) -> None:
        """The destination ends up holding exactly what the publish wrote to staging."""
        packager = WorkflowPackager("test_workflow")
        destination = tmp_path / "bundle"

        with packager.staged_publish(destination) as staging:
            (staging / "run.py").write_text("new", encoding="utf-8")

        assert (destination / "run.py").read_text(encoding="utf-8") == "new"

    def test_removes_artifacts_absent_from_the_new_publish(self, tmp_path: Path) -> None:
        """A file left by an earlier publish does not survive into the new bundle."""
        packager = WorkflowPackager("test_workflow")
        destination = tmp_path / "bundle"
        destination.mkdir()
        (destination / DOWNLOAD_MODELS_SCRIPT_NAME).write_text("stale", encoding="utf-8")
        (destination / ".env").write_text("GT_CLOUD_API_KEY=''\n", encoding="utf-8")

        with packager.staged_publish(destination) as staging:
            (staging / ".env").write_text("GT_CLOUD_API_KEY='real-key'\n", encoding="utf-8")

        assert not (destination / DOWNLOAD_MODELS_SCRIPT_NAME).exists()
        assert (destination / ".env").read_text(encoding="utf-8") == "GT_CLOUD_API_KEY='real-key'\n"

    def test_leaves_previous_bundle_intact_on_failure(self, tmp_path: Path) -> None:
        """A publish that raises partway through does not touch the destination."""
        packager = WorkflowPackager("test_workflow")
        destination = tmp_path / "bundle"
        destination.mkdir()
        (destination / "run.py").write_text("previous", encoding="utf-8")

        def publish_and_fail() -> None:
            with packager.staged_publish(destination) as staging:
                (staging / "run.py").write_text("half-written", encoding="utf-8")
                msg = "publish failed partway through"
                raise RuntimeError(msg)

        with pytest.raises(RuntimeError):
            publish_and_fail()

        assert (destination / "run.py").read_text(encoding="utf-8") == "previous"

    def test_cleans_up_the_staging_directory(self, tmp_path: Path) -> None:
        """The staging directory does not outlive the publish, successful or not."""
        packager = WorkflowPackager("test_workflow")
        destination = tmp_path / "bundle"

        with packager.staged_publish(destination) as staging:
            (staging / "run.py").write_text("new", encoding="utf-8")
            staging_path = staging

        assert not staging_path.exists()

    def test_carries_preserved_entries_across_the_swap(self, tmp_path: Path) -> None:
        """Opted-in entries (e.g. per-version subdirs) survive a rebuild of the bundle."""
        packager = WorkflowPackager("test_workflow")
        destination = tmp_path / "bundle"
        (destination / "v1").mkdir(parents=True)
        (destination / "v1" / "workflow.py").write_text("v1", encoding="utf-8")
        (destination / "stale.py").write_text("stale", encoding="utf-8")

        with packager.staged_publish(destination, preserve=["v1", "v2"]) as staging:
            (staging / "run.py").write_text("new", encoding="utf-8")

        assert (destination / "v1" / "workflow.py").read_text(encoding="utf-8") == "v1"
        assert (destination / "run.py").exists()
        assert not (destination / "stale.py").exists()

    def test_carries_entries_matching_a_preserve_pattern(self, tmp_path: Path) -> None:
        """A pattern preserves an open-ended set of entries without enumerating them."""
        packager = WorkflowPackager("test_workflow")
        destination = tmp_path / "bundle"
        for version in ("v1", "v2", "v3"):
            (destination / version).mkdir(parents=True)
            (destination / version / "workflow.py").write_text(version, encoding="utf-8")
        (destination / "stale.py").write_text("stale", encoding="utf-8")

        with packager.staged_publish(destination, preserve=["v*"]) as staging:
            (staging / "run.py").write_text("new", encoding="utf-8")

        for version in ("v1", "v2", "v3"):
            assert (destination / version / "workflow.py").read_text(encoding="utf-8") == version
        assert not (destination / "stale.py").exists()

    def test_preserve_pattern_does_not_reach_outside_the_bundle(self, tmp_path: Path) -> None:
        """Patterns match the bundle's own entries, so traversal cannot pull in a sibling."""
        packager = WorkflowPackager("test_workflow")
        destination = tmp_path / "bundle"
        destination.mkdir()
        (tmp_path / "outside.py").write_text("outside", encoding="utf-8")

        with packager.staged_publish(destination, preserve=["../outside.py", "*"]) as staging:
            (staging / "run.py").write_text("new", encoding="utf-8")

        assert not (destination / "outside.py").exists()
        assert not (destination / ".." / "bundle" / "outside.py").exists()
        assert (tmp_path / "outside.py").exists()

    def test_publish_written_entry_wins_over_a_preserved_name(self, tmp_path: Path) -> None:
        """A name the publish itself wrote is not overwritten by the previous bundle's copy."""
        packager = WorkflowPackager("test_workflow")
        destination = tmp_path / "bundle"
        (destination / "v1").mkdir(parents=True)
        (destination / "v1" / "workflow.py").write_text("old", encoding="utf-8")

        with packager.staged_publish(destination, preserve=["v1"]) as staging:
            (staging / "v1").mkdir()
            (staging / "v1" / "workflow.py").write_text("new", encoding="utf-8")

        assert (destination / "v1" / "workflow.py").read_text(encoding="utf-8") == "new"

    def test_creates_a_destination_that_does_not_exist_yet(self, tmp_path: Path) -> None:
        """A first publish into a fresh path works, including missing parent directories."""
        packager = WorkflowPackager("test_workflow")
        destination = tmp_path / "nested" / "bundle"

        with packager.staged_publish(destination) as staging:
            (staging / "run.py").write_text("new", encoding="utf-8")

        assert (destination / "run.py").read_text(encoding="utf-8") == "new"

    def test_restores_the_previous_bundle_if_the_swap_fails(self, tmp_path: Path) -> None:
        """A failure moving the new bundle into place leaves the destination populated."""
        packager = WorkflowPackager("test_workflow")
        destination = tmp_path / "bundle"
        destination.mkdir()
        (destination / "run.py").write_text("previous", encoding="utf-8")

        real_handle_request = GriptapeNodes.handle_request
        staged_bundle_move_attempted = False

        def fail_moving_new_bundle_into_place(request: object) -> object:
            """Refuse the staging -> destination move; let the aside and restore moves through."""
            nonlocal staged_bundle_move_attempted
            moving_into_destination = isinstance(request, RenameFileRequest) and request.new_path == str(destination)
            if moving_into_destination and not staged_bundle_move_attempted:
                staged_bundle_move_attempted = True
                return RenameFileResultFailure(
                    failure_reason=FileIOFailureReason.PERMISSION_DENIED,
                    result_details="simulated rename failure",
                )
            return real_handle_request(request)  # type: ignore[arg-type]

        def publish_with_failing_swap() -> None:
            with (
                patch(
                    "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.handle_request",
                    side_effect=fail_moving_new_bundle_into_place,
                ),
                packager.staged_publish(destination) as staging,
            ):
                (staging / "run.py").write_text("new", encoding="utf-8")

        with pytest.raises(TypeError):
            publish_with_failing_swap()

        assert (destination / "run.py").read_text(encoding="utf-8") == "previous"

    def test_keeps_the_moved_aside_bundle_when_rollback_fails(self, tmp_path: Path) -> None:
        """If the destination cannot be restored, the only copy of the bundle is not deleted."""
        packager = WorkflowPackager("test_workflow")
        destination = tmp_path / "bundle"
        destination.mkdir()
        (destination / "run.py").write_text("previous", encoding="utf-8")

        real_handle_request = GriptapeNodes.handle_request

        def fail_every_move_to_the_destination(request: object) -> object:
            """Refuse both the swap and the rollback, leaving the destination missing."""
            if isinstance(request, RenameFileRequest) and request.new_path == str(destination):
                return RenameFileResultFailure(
                    failure_reason=FileIOFailureReason.PERMISSION_DENIED,
                    result_details="simulated rename failure",
                )
            return real_handle_request(request)  # type: ignore[arg-type]

        def publish_with_failing_swap_and_rollback() -> None:
            with (
                patch(
                    "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.handle_request",
                    side_effect=fail_every_move_to_the_destination,
                ),
                packager.staged_publish(destination) as staging,
            ):
                (staging / "run.py").write_text("new", encoding="utf-8")

        with pytest.raises(TypeError):
            publish_with_failing_swap_and_rollback()

        moved_aside = list(tmp_path.glob("bundle.publish-*.previous"))
        assert len(moved_aside) == 1
        assert (moved_aside[0] / "run.py").read_text(encoding="utf-8") == "previous"

    def test_failure_names_the_destination_not_a_working_directory(self, tmp_path: Path) -> None:
        """Every swap failure names the bundle the user published to, whichever move failed."""
        packager = WorkflowPackager("test_workflow")
        destination = tmp_path / "bundle"
        destination.mkdir()
        (destination / "run.py").write_text("previous", encoding="utf-8")

        real_handle_request = GriptapeNodes.handle_request

        def fail_moving_the_previous_bundle_aside(request: object) -> object:
            """Refuse the destination -> aside move, whose target is an internal path."""
            if isinstance(request, RenameFileRequest) and request.old_path == str(destination):
                return RenameFileResultFailure(
                    failure_reason=FileIOFailureReason.PERMISSION_DENIED,
                    result_details="simulated rename failure",
                )
            return real_handle_request(request)  # type: ignore[arg-type]

        def publish_with_failing_move_aside() -> None:
            with (
                patch(
                    "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.handle_request",
                    side_effect=fail_moving_the_previous_bundle_aside,
                ),
                packager.staged_publish(destination) as staging,
            ):
                (staging / "run.py").write_text("new", encoding="utf-8")

        with pytest.raises(TypeError) as failure:
            publish_with_failing_move_aside()

        assert f"'{destination}'" in str(failure.value)
        assert ".publish-" not in str(failure.value)

    def test_cleanup_failure_does_not_fail_the_publish(self, tmp_path: Path) -> None:
        """A working directory that cannot be removed is logged, not raised over."""
        packager = WorkflowPackager("test_workflow")
        destination = tmp_path / "bundle"
        destination.mkdir()
        (destination / "run.py").write_text("previous", encoding="utf-8")

        real_handle_request = GriptapeNodes.handle_request

        def fail_deletes(request: object) -> object:
            """Report every delete as failed, leaving the working directories on disk."""
            if isinstance(request, DeleteFileRequest):
                return MagicMock()
            return real_handle_request(request)  # type: ignore[arg-type]

        with (
            patch(
                "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.handle_request",
                side_effect=fail_deletes,
            ),
            packager.staged_publish(destination) as staging,
        ):
            (staging / "run.py").write_text("new", encoding="utf-8")

        assert (destination / "run.py").read_text(encoding="utf-8") == "new"

    def test_swap_routes_through_engine_requests(self, tmp_path: Path) -> None:
        """Staging directory creation and the swap go through OS request handlers."""
        packager = WorkflowPackager("test_workflow")
        destination = tmp_path / "bundle"
        destination.mkdir()
        (destination / "run.py").write_text("previous", encoding="utf-8")
        seen: list[type] = []

        real_handle_request = GriptapeNodes.handle_request

        def record(request: object) -> object:
            seen.append(type(request))
            return real_handle_request(request)  # type: ignore[arg-type]

        with (
            patch(
                "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.handle_request",
                side_effect=record,
            ),
            packager.staged_publish(destination) as staging,
        ):
            (staging / "run.py").write_text("new", encoding="utf-8")

        # Two renames: the previous bundle aside, then staging into place.
        expected_renames = 2
        assert MakeDirectoryRequest in seen
        assert seen.count(RenameFileRequest) == expected_renames
        assert (destination / "run.py").read_text(encoding="utf-8") == "new"
