"""Tests for WorkflowPackager: library dependency resolution and clean-rebuild publishing."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from dotenv import dotenv_values

from griptape_nodes.node_library.library_registry import LibraryNameAndVersion
from griptape_nodes.retained_mode.events.os_events import DeleteFileResultSuccess, WriteFileResultSuccess
from griptape_nodes.retained_mode.events.secrets_events import GetAllSecretValuesResultSuccess
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


class TestCopyStaticFiles:
    """copy_static_files handles the case where the destination resolves back onto the source."""

    def test_skips_copy_when_source_and_dest_are_same_file(self, tmp_path: Path) -> None:
        """A file whose destination resolves to itself is left in place instead of copied."""
        packager = WorkflowPackager("test_workflow")
        source = tmp_path / "inputs" / "images" / "img.jpg"
        source.parent.mkdir(parents=True)
        source.write_text("data")
        relative = Path("inputs/images/img.jpg")

        # destination is the project root itself, so dest == source.
        with (
            patch.object(packager, "_resolve_file_reference", return_value=(source, relative)),
            patch(
                "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.handle_request",
                return_value=MagicMock(),
            ),
            patch.object(packager, "copy_file") as mock_copy_file,
            patch.object(packager, "copy_tree") as mock_copy_tree,
        ):
            packager.copy_static_files([("node", "img.jpg")], tmp_path)

        mock_copy_file.assert_not_called()
        mock_copy_tree.assert_not_called()

    def test_copies_when_source_and_dest_differ(self, tmp_path: Path) -> None:
        """A file whose destination differs from the source is copied."""
        packager = WorkflowPackager("test_workflow")
        source = tmp_path / "inputs" / "images" / "img.jpg"
        source.parent.mkdir(parents=True)
        source.write_text("data")
        relative = Path("inputs/images/img.jpg")
        destination = tmp_path / "bundle"

        with (
            patch.object(packager, "_resolve_file_reference", return_value=(source, relative)),
            patch(
                "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.handle_request",
                return_value=MagicMock(),
            ),
            patch.object(packager, "copy_file") as mock_copy_file,
            patch.object(packager, "copy_tree") as mock_copy_tree,
        ):
            packager.copy_static_files([("node", "img.jpg")], destination)

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
    """get_process_env_secrets picks up registered secrets that exist only in the environment."""

    def test_returns_registered_secret_set_only_in_the_environment(self) -> None:
        """A registered secret exported in the shell is available to the bundle."""
        secrets_manager = MagicMock(secrets_to_register={"GT_CLOUD_API_KEY": ""})

        with (
            patch(
                "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.SecretsManager",
                return_value=secrets_manager,
            ),
            patch.dict("os.environ", {"GT_CLOUD_API_KEY": "from-shell"}, clear=False),
        ):
            result = WorkflowPackager.get_process_env_secrets(exclude=set())

        assert result == {"GT_CLOUD_API_KEY": "from-shell"}

    def test_skips_excluded_and_unregistered_keys(self) -> None:
        """Keys already sourced from a .env file, and unregistered env vars, are left alone."""
        secrets_manager = MagicMock(secrets_to_register={"GT_CLOUD_API_KEY": ""})

        with (
            patch(
                "griptape_nodes.retained_mode.publishing.workflow_packager.GriptapeNodes.SecretsManager",
                return_value=secrets_manager,
            ),
            patch.dict("os.environ", {"GT_CLOUD_API_KEY": "from-shell", "UNRELATED_SECRET": "nope"}, clear=False),
        ):
            result = WorkflowPackager.get_process_env_secrets(exclude={"GT_CLOUD_API_KEY"})

        assert result == {}

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
            result = WorkflowPackager.get_process_env_secrets(exclude=set())

        assert result == {}


class TestWriteDownloadModelsScript:
    """A workflow with no HuggingFace models leaves no download script behind."""

    def test_removes_stale_script_when_no_models_are_needed(self, tmp_path: Path) -> None:
        """A script from an earlier publish is deleted, not left to run again."""
        packager = WorkflowPackager("test_workflow")
        script_path = tmp_path / "download_models.py"
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
        (tmp_path / "download_models.py").write_text("# from an earlier publish\n", encoding="utf-8")

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
        (destination / "download_models.py").write_text("stale", encoding="utf-8")
        (destination / ".env").write_text("GT_CLOUD_API_KEY=''\n", encoding="utf-8")

        with packager.staged_publish(destination) as staging:
            (staging / ".env").write_text("GT_CLOUD_API_KEY='real-key'\n", encoding="utf-8")

        assert not (destination / "download_models.py").exists()
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
        """A copy failure during the swap leaves the destination populated, not missing."""
        packager = WorkflowPackager("test_workflow")
        destination = tmp_path / "bundle"
        destination.mkdir()
        (destination / "run.py").write_text("previous", encoding="utf-8")

        def publish_with_failing_copy() -> None:
            with (
                patch(
                    "griptape_nodes.retained_mode.publishing.workflow_packager.shutil.copytree",
                    side_effect=OSError("disk full"),
                ),
                packager.staged_publish(destination) as staging,
            ):
                (staging / "run.py").write_text("new", encoding="utf-8")

        with pytest.raises(TypeError):
            publish_with_failing_copy()

        assert (destination / "run.py").read_text(encoding="utf-8") == "previous"
