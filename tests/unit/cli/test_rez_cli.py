"""Unit tests for the ``gtn rez`` CLI commands."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from griptape_nodes.cli.commands import rez
from griptape_nodes.cli.commands.rez import (
    _admin_path,
    _build_library_from_dir,
    _build_library_from_git,
    _build_library_from_local,
    _check_rez_bindings,
    _copy_dist_info,
    _format_path_map,
    _install_deps,
    _print_env_var_guidance,
    _print_library_next_steps,
    _prompt_rez_paths,
    _prompt_studio_setup,
    _read_dependencies,
    _read_project_metadata,
    _read_version,
    _resolve_and_validate_repo,
    _resolve_engine_repo,
    _resolve_studio_root_from_env_or_option,
    _validate_rez_package,
    _warn_if_rez_does_not_search,
    _write_engine_package,
    app,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

MODULE = "griptape_nodes.cli.commands.rez"
PYTHON_FAMILY = f"python-{sys.version_info.major}.{sys.version_info.minor}"

runner = CliRunner()


@pytest.fixture(autouse=True)
def rez_searches_every_store() -> Iterator[MagicMock]:
    """Default: rez searches every store, so no test depends on a local rez-config binary."""
    with patch(f"{MODULE}.rez_unsearched_stores", return_value=[]) as unsearched:
        yield unsearched


@pytest.fixture
def output() -> Iterator[io.StringIO]:
    """Route the CLI's rich console into a wide in-memory buffer so text assertions are stable."""
    buffer = io.StringIO()
    with patch(f"{MODULE}.console", Console(file=buffer, width=400, force_terminal=False, color_system=None)):
        yield buffer


@pytest.fixture
def clean_env() -> Iterator[None]:
    """Isolate os.environ; the library builder sets GTN_REZ_LOCAL_PACKAGES_PATH as a side effect."""
    with patch.dict(os.environ, {}, clear=True):
        yield


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _write_pyproject(repo: Path, *, version: str | None = "1.2.3", deps: list[str] | None = None) -> Path:
    lines = ["[project]", 'name = "griptape-nodes-engine"']
    if version is not None:
        lines.append(f'version = "{version}"')
    lines.append(f"dependencies = {json.dumps(deps or [])}")
    path = repo / "pyproject.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _make_engine_repo(tmp_path: Path, *, version: str = "1.2.3", deps: list[str] | None = None) -> Path:
    repo = tmp_path / "engine"
    (repo / "src" / "griptape_nodes").mkdir(parents=True)
    (repo / "src" / "griptape_nodes" / "__init__.py").write_text("", encoding="utf-8")
    _write_pyproject(repo, version=version, deps=deps)
    return repo


def _make_library(tmp_path: Path, manifest: dict) -> Path:
    library_dir = tmp_path / "my_library"
    library_dir.mkdir()
    (library_dir / "griptape_nodes_library.json").write_text(json.dumps(manifest), encoding="utf-8")
    return library_dir


# ---------------------------------------------------------------------------
# Rez-bind safety check
# ---------------------------------------------------------------------------


class TestCheckRezBindings:
    def test_all_bindings_present_returns_silently(self, output: io.StringIO) -> None:
        with (
            patch(f"{MODULE}._rez_executable", return_value="rez-search"),
            patch(f"{MODULE}.subprocess.run", return_value=_completed(0)) as run,
            patch(f"{MODULE}.typer.confirm") as confirm,
        ):
            _check_rez_bindings()

        searched = [call.args[0][1] for call in run.call_args_list]
        assert searched == ["platform", "os", PYTHON_FAMILY]
        confirm.assert_not_called()
        assert "Missing Rez Bindings" not in output.getvalue()

    def test_missing_bindings_abort_by_default(self, output: io.StringIO) -> None:
        with (
            patch(f"{MODULE}._rez_executable", return_value="rez-search"),
            patch(f"{MODULE}.subprocess.run", return_value=_completed(1)),
            patch(f"{MODULE}.typer.confirm", return_value=False) as confirm,
            pytest.raises(typer.Abort),
        ):
            _check_rez_bindings()

        assert confirm.call_args.kwargs["default"] is False
        text = output.getvalue()
        assert "rez bind platform" in text
        assert "rez bind os" in text
        assert f"rez bind python (version {sys.version_info.major}.{sys.version_info.minor})" in text

    def test_missing_bindings_continue_when_confirmed(self, output: io.StringIO) -> None:
        with (
            patch(f"{MODULE}._rez_executable", return_value="rez-search"),
            patch(f"{MODULE}.subprocess.run", return_value=_completed(1)),
            patch(f"{MODULE}.typer.confirm", return_value=True),
        ):
            _check_rez_bindings()

        assert "Missing Rez Bindings" in output.getvalue()

    def test_non_interactive_warns_and_proceeds(self, output: io.StringIO) -> None:
        with (
            patch(f"{MODULE}._rez_executable", return_value="rez-search"),
            patch(f"{MODULE}.subprocess.run", return_value=_completed(1)),
            patch(f"{MODULE}.typer.confirm") as confirm,
        ):
            _check_rez_bindings(interactive=False)

        confirm.assert_not_called()
        assert "Proceeding without bindings" in output.getvalue()

    def test_only_missing_families_are_listed(self, output: io.StringIO) -> None:
        def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            return _completed(0 if cmd[1] == "platform" else 1)

        with (
            patch(f"{MODULE}._rez_executable", return_value="rez-search"),
            patch(f"{MODULE}.subprocess.run", side_effect=fake_run),
        ):
            _check_rez_bindings(interactive=False)

        text = output.getvalue()
        assert "rez bind platform" not in text
        assert "rez bind os" in text

    def test_rez_search_errors_count_as_missing(self, output: io.StringIO) -> None:
        with (
            patch(f"{MODULE}._rez_executable", return_value="rez-search"),
            patch(f"{MODULE}.subprocess.run", side_effect=OSError("rez-search not found")),
        ):
            _check_rez_bindings(interactive=False)

        assert "rez bind platform" in output.getvalue()


# ---------------------------------------------------------------------------
# pyproject helpers
# ---------------------------------------------------------------------------


class TestPyprojectReading:
    def test_read_version_from_project_table(self, tmp_path: Path) -> None:
        path = _write_pyproject(tmp_path, version="0.9.1")
        assert _read_version(path) == "0.9.1"

    def test_read_version_from_poetry_table(self, tmp_path: Path) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_text('[tool.poetry]\nversion = "2.0.0"\n', encoding="utf-8")
        assert _read_version(path) == "2.0.0"

    def test_read_version_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_text('[project]\nname = "x"\n', encoding="utf-8")
        assert _read_version(path) is None

    def test_read_dependencies(self, tmp_path: Path) -> None:
        path = _write_pyproject(tmp_path, deps=["requests>=2", "rich"])
        assert _read_dependencies(path) == ["requests>=2", "rich"]

    def test_read_dependencies_absent(self, tmp_path: Path) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_text('[project]\nname = "x"\n', encoding="utf-8")
        assert _read_dependencies(path) == []

    def test_read_project_metadata(self, tmp_path: Path, output: io.StringIO) -> None:
        path = _write_pyproject(tmp_path, version="3.1.0", deps=["a", "b"])
        assert _read_project_metadata(path) == ("3.1.0", ["a", "b"])
        assert "3.1.0" in output.getvalue()

    @pytest.mark.usefixtures("output")
    def test_read_project_metadata_without_version_exits(self, tmp_path: Path) -> None:
        path = _write_pyproject(tmp_path, version=None)
        with pytest.raises(typer.Exit):
            _read_project_metadata(path)


# ---------------------------------------------------------------------------
# Engine repo detection
# ---------------------------------------------------------------------------


class TestAdminPath:
    def test_strips_windows_long_path_prefix(self) -> None:
        # canonicalize_for_io always prefixes on Windows; the prefix must not reach
        # paths that are printed back to the admin or written into GTN_REZ_* guidance.
        with patch(f"{MODULE}.canonicalize_for_io", return_value=Path("\\\\?\\C:\\studio")):
            assert str(_admin_path("C:\\studio")) == "C:\\studio"

    def test_expands_home(self) -> None:
        assert _admin_path("~/studio") == Path.home() / "studio"


class TestResolveEngineRepo:
    def test_explicit_existing_path(self, tmp_path: Path) -> None:
        assert _resolve_engine_repo(str(tmp_path)) == tmp_path

    def test_explicit_missing_path(self, tmp_path: Path, output: io.StringIO) -> None:
        assert _resolve_engine_repo(str(tmp_path / "nope")) is None
        assert "Engine repo not found" in output.getvalue()

    def test_detects_checkout_from_package_location(self) -> None:
        # The test suite runs from a source checkout, so the package location gives the repo root.
        repo = _resolve_engine_repo(None)
        assert repo is not None
        assert (repo / "pyproject.toml").exists()
        assert (repo / "src" / "griptape_nodes").is_dir()

    def test_falls_back_to_env_repo(self, tmp_path: Path) -> None:
        fake_pkg = tmp_path / "site" / "griptape_nodes" / "__init__.py"
        repo = _make_engine_repo(tmp_path)
        with (
            patch(f"{MODULE}.griptape_nodes.__file__", str(fake_pkg)),
            patch.dict(os.environ, {"GRIPTAPE_ENGINE_REPO": str(repo)}),
        ):
            assert _resolve_engine_repo(None) == repo.resolve()

    def test_nothing_found(self, tmp_path: Path, output: io.StringIO) -> None:
        fake_pkg = tmp_path / "site" / "griptape_nodes" / "__init__.py"
        with (
            patch(f"{MODULE}.griptape_nodes.__file__", str(fake_pkg)),
            patch(f"{MODULE}.Path.home", return_value=tmp_path / "home"),
            patch(f"{MODULE}.Path.cwd", return_value=tmp_path / "cwd"),
            patch.dict(os.environ, {}, clear=True),
        ):
            assert _resolve_engine_repo(None) is None
        assert "Could not auto-detect engine repo" in output.getvalue()


class TestResolveAndValidateRepo:
    @pytest.mark.usefixtures("output")
    def test_returns_repo_and_pyproject(self, tmp_path: Path) -> None:
        repo = _make_engine_repo(tmp_path)
        assert _resolve_and_validate_repo(str(repo)) == (repo, repo / "pyproject.toml")

    @pytest.mark.usefixtures("output")
    def test_prompts_when_not_found(self, tmp_path: Path) -> None:
        repo = _make_engine_repo(tmp_path)
        with (
            patch(f"{MODULE}._resolve_engine_repo", return_value=None),
            patch(f"{MODULE}.typer.prompt", return_value=str(repo)),
        ):
            repo_path, pyproject = _resolve_and_validate_repo(None)
        assert pyproject == repo_path / "pyproject.toml"
        assert pyproject.exists()

    @pytest.mark.usefixtures("output")
    def test_prompted_directory_missing_exits(self, tmp_path: Path) -> None:
        with (
            patch(f"{MODULE}._resolve_engine_repo", return_value=None),
            patch(f"{MODULE}.typer.prompt", return_value=str(tmp_path / "missing")),
            pytest.raises(typer.Exit),
        ):
            _resolve_and_validate_repo(None)

    @pytest.mark.usefixtures("output")
    def test_missing_pyproject_exits(self, tmp_path: Path) -> None:
        with pytest.raises(typer.Exit):
            _resolve_and_validate_repo(str(tmp_path))


# ---------------------------------------------------------------------------
# Studio root setup
# ---------------------------------------------------------------------------


class TestStudioRootNonInteractive:
    def test_packages_path_option(self, tmp_path: Path) -> None:
        root, paths = _resolve_studio_root_from_env_or_option(str(tmp_path))
        assert root == tmp_path
        assert paths["local_packages"] == "local"

    def test_env_root(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"GTN_REZ_ROOT": str(tmp_path)}):
            root, paths = _resolve_studio_root_from_env_or_option(None)
        assert root == tmp_path
        assert paths["local_packages"] == "rez/packages/local"

    @pytest.mark.usefixtures("output", "clean_env")
    def test_nothing_set_exits(self) -> None:
        with pytest.raises(typer.Exit):
            _resolve_studio_root_from_env_or_option(None)


class TestPromptStudioSetup:
    @pytest.mark.usefixtures("output")
    def test_uses_existing_env_without_prompting(self, tmp_path: Path) -> None:
        env = {
            "GTN_REZ_ROOT": str(tmp_path),
            "GTN_REZ_PATH_MAP": f"linux=/mnt/p;osx={tmp_path}",
            "GTN_REZ_LOCAL_PACKAGES_PATH": "pkgs/local",
        }
        with patch.dict(os.environ, env, clear=True), patch(f"{MODULE}.typer.prompt") as prompt:
            roots, studio_root, paths = _prompt_studio_setup()
        prompt.assert_not_called()
        assert studio_root == tmp_path
        assert roots == {"linux": "/mnt/p", "osx": str(tmp_path)}
        assert paths["local_packages"] == "pkgs/local"
        assert paths["bin"] == "rez/bin"

    @pytest.mark.usefixtures("output", "clean_env")
    def test_single_platform_prompts(self, tmp_path: Path) -> None:
        with (
            patch(f"{MODULE}.typer.confirm", return_value=False),
            patch(f"{MODULE}.typer.prompt", return_value=str(tmp_path)),
            patch(f"{MODULE}._prompt_rez_paths", return_value={"local_packages": "x"}) as rez_paths,
        ):
            roots, studio_root, paths = _prompt_studio_setup()
        assert roots is None
        assert studio_root == tmp_path
        assert paths == {"local_packages": "x"}
        rez_paths.assert_called_once()

    @pytest.mark.usefixtures("output", "clean_env")
    def test_cross_platform_uses_current_platform_root(self) -> None:
        answers = iter(["/mnt/pipeline", "P:", "/Volumes/pipeline"])
        with (
            patch(f"{MODULE}.typer.confirm", return_value=True),
            patch(f"{MODULE}.typer.prompt", side_effect=lambda *_a, **_k: next(answers)),
            patch(f"{MODULE}.current_platform_key", return_value="osx"),
            patch(f"{MODULE}._prompt_rez_paths", return_value={}),
        ):
            roots, studio_root, _ = _prompt_studio_setup()
        assert roots == {"linux": "/mnt/pipeline", "windows": "P:", "osx": "/Volumes/pipeline"}
        assert studio_root == Path("/Volumes/pipeline")

    @pytest.mark.usefixtures("clean_env")
    def test_cross_platform_falls_back_to_first_entry(self, output: io.StringIO) -> None:
        answers = iter(["/mnt/pipeline", "", ""])
        with (
            patch(f"{MODULE}.typer.confirm", return_value=True),
            patch(f"{MODULE}.typer.prompt", side_effect=lambda *_a, **_k: next(answers)),
            patch(f"{MODULE}.current_platform_key", return_value="osx"),
            patch(f"{MODULE}._prompt_rez_paths", return_value={}),
        ):
            roots, studio_root, _ = _prompt_studio_setup()
        assert roots == {"linux": "/mnt/pipeline"}
        assert studio_root == Path("/mnt/pipeline")
        assert "not in mapping" in output.getvalue()

    @pytest.mark.usefixtures("output", "clean_env")
    def test_cross_platform_with_no_roots_exits(self) -> None:
        with (
            patch(f"{MODULE}.typer.confirm", return_value=True),
            patch(f"{MODULE}.typer.prompt", return_value=""),
            pytest.raises(typer.Exit),
        ):
            _prompt_studio_setup()


class TestPromptRezPaths:
    @pytest.mark.usefixtures("output")
    def test_relative_defaults(self) -> None:
        with (
            patch(f"{MODULE}.typer.confirm", return_value=False),
            patch(f"{MODULE}.typer.prompt", side_effect=lambda _label, default="": default),
        ):
            paths = _prompt_rez_paths()
        assert paths == {
            "bin": "rez/bin",
            "config_file": "rez/rezconfig.py",
            "local_packages": "rez/packages/local",
            "release_packages": "rez/packages/release",
        }

    @pytest.mark.usefixtures("output")
    def test_absolute_local_packages(self, tmp_path: Path) -> None:
        local = tmp_path / "local"

        def fake_prompt(label: str, default: str = "") -> str:
            if "absolute path" in label:
                return str(local)
            return default

        with (
            patch(f"{MODULE}.typer.confirm", return_value=True),
            patch(f"{MODULE}.typer.prompt", side_effect=fake_prompt),
        ):
            paths = _prompt_rez_paths()
        assert paths["local_packages"] == str(local)
        assert paths["bin"] == "rez/bin"


# ---------------------------------------------------------------------------
# Dependency install and engine package writing
# ---------------------------------------------------------------------------


class TestInstallDeps:
    def test_no_dependencies_skips(self, tmp_path: Path, output: io.StringIO) -> None:
        with patch(f"{MODULE}.rez_uv_install") as install:
            _install_deps([], tmp_path, skip_installed=True)
        install.assert_not_called()
        assert "skipping dependency install" in output.getvalue()

    @pytest.mark.usefixtures("output")
    def test_installs_into_store(self, tmp_path: Path) -> None:
        with patch(f"{MODULE}.rez_uv_install") as install:
            _install_deps(["requests"], tmp_path, skip_installed=False)
        install.assert_called_once_with(["requests"], packages_dir=tmp_path, skip_installed=False)

    def test_resolve_failure_shows_uv_error_and_exits(self, tmp_path: Path, output: io.StringIO) -> None:
        error = subprocess.CalledProcessError(1, "uv", stderr="No solution found when resolving requests")
        with (
            patch(f"{MODULE}.rez_uv_install", side_effect=error),
            pytest.raises(typer.Exit),
        ):
            _install_deps(["requests"], tmp_path, skip_installed=True)
        text = output.getvalue()
        assert "Attempted to install the engine's dependencies as rez packages" in text
        assert "No solution found when resolving requests" in text

    def test_resolve_failure_without_stderr_shows_exit_status(self, tmp_path: Path, output: io.StringIO) -> None:
        with (
            patch(f"{MODULE}.rez_uv_install", side_effect=subprocess.CalledProcessError(2, "uv")),
            pytest.raises(typer.Exit),
        ):
            _install_deps(["requests"], tmp_path, skip_installed=True)
        assert "uv exited with status 2" in output.getvalue()

    def test_filesystem_failure_exits(self, tmp_path: Path, output: io.StringIO) -> None:
        with (
            patch(f"{MODULE}.rez_uv_install", side_effect=PermissionError("store is read-only")),
            pytest.raises(typer.Exit),
        ):
            _install_deps(["requests"], tmp_path, skip_installed=True)
        assert "Failed due to: store is read-only" in output.getvalue()


class TestWriteEnginePackage:
    @pytest.mark.usefixtures("output")
    def test_writes_package_source_and_dist_info(self, tmp_path: Path) -> None:
        repo = _make_engine_repo(tmp_path)
        (repo / "src" / "griptape_nodes" / "__pycache__").mkdir()
        store = tmp_path / "store"
        with patch(f"{MODULE}.build_direct_requires", return_value=["requests-2.32.3", "attrs-24.0.0"]):
            _write_engine_package(repo, store, "1.2.3", ["requests", "attrs"])

        version_dir = store / "local" / "griptape_nodes_engine" / "1.2.3"
        package_py = (version_dir / "package.py").read_text(encoding="utf-8")
        assert "name = 'griptape_nodes_engine'" in package_py
        assert "version = '1.2.3'" in package_py
        assert package_py.index("'attrs-24.0.0'") < package_py.index("'requests-2.32.3'")
        assert "format_version = 2" in package_py
        assert "env.PYTHONPATH.append('{root}/python')" in package_py
        assert (version_dir / "python" / "griptape_nodes" / "__init__.py").exists()
        assert not (version_dir / "python" / "griptape_nodes" / "__pycache__").exists()
        metadata = (version_dir / "python" / "griptape_nodes_engine-1.2.3.dist-info" / "METADATA").read_text()
        assert "Version: 1.2.3" in metadata

    def test_overwrites_existing_version(self, tmp_path: Path, output: io.StringIO) -> None:
        repo = _make_engine_repo(tmp_path)
        store = tmp_path / "store"
        stale = store / "local" / "griptape_nodes_engine" / "1.2.3" / "stale.txt"
        stale.parent.mkdir(parents=True)
        stale.write_text("old", encoding="utf-8")
        with patch(f"{MODULE}.build_direct_requires", return_value=[]):
            _write_engine_package(repo, store, "1.2.3", [])
        assert not stale.exists()
        assert "overwriting" in output.getvalue()

    @pytest.mark.usefixtures("output")
    def test_missing_src_exits(self, tmp_path: Path) -> None:
        repo = tmp_path / "engine"
        repo.mkdir()
        with pytest.raises(typer.Exit):
            _write_engine_package(repo, tmp_path / "store", "1.0.0", [])

    @pytest.mark.usefixtures("output")
    def test_copy_dist_info_contents(self, tmp_path: Path) -> None:
        _copy_dist_info(tmp_path, "4.5.6")
        dist_info = tmp_path / "griptape_nodes_engine-4.5.6.dist-info"
        assert (dist_info / "top_level.txt").read_text() == "griptape_nodes\n"
        assert (dist_info / "INSTALLER").read_text() == "gtn-rez\n"


# ---------------------------------------------------------------------------
# Guidance output
# ---------------------------------------------------------------------------


class TestEnvVarGuidance:
    PATHS = {  # noqa: RUF012
        "bin": "rez/bin",
        "config_file": "rez/rezconfig.py",
        "local_packages": "rez/packages/local",
        "release_packages": "rez/packages/release",
    }

    def test_format_path_map_sorted(self) -> None:
        assert _format_path_map({"osx": "/Volumes/p", "linux": "/mnt/p"}) == "linux=/mnt/p;osx=/Volumes/p"

    def test_single_platform(self, tmp_path: Path, output: io.StringIO) -> None:
        _print_env_var_guidance(tmp_path, self.PATHS)
        text = output.getvalue()
        assert "GTN_REZ_PATH_MAP" not in text
        assert f"env.GTN_REZ_ROOT = '{tmp_path}'" in text
        assert "env.GTN_REZ_LOCAL_PACKAGES = 'rez/packages/local'" in text
        assert "env.GTN_REZ_CONFIG_FILE = 'rez/rezconfig.py'" in text

    def test_cross_platform(self, tmp_path: Path, output: io.StringIO) -> None:
        roots = {"linux": "/mnt/p", "windows": "P:"}
        _print_env_var_guidance(tmp_path, self.PATHS, cross_platform_roots=roots)
        text = output.getvalue()
        assert "env.GTN_REZ_PATH_MAP = 'linux=/mnt/p;windows=P:'" in text
        assert "env.GTN_REZ_ROOT = _roots.get(system.platform, '')" in text

    def test_library_next_steps_with_and_without_version(self, output: io.StringIO) -> None:
        _print_library_next_steps("my_lib", "1.0.0")
        _print_library_next_steps("other_lib", None)
        text = output.getvalue()
        assert "REZ:my_lib-1.0.0" in text
        assert '"REZ:other_lib"' in text


# ---------------------------------------------------------------------------
# Package validation
# ---------------------------------------------------------------------------


class TestValidateRezPackage:
    def test_found_and_resolves(self, output: io.StringIO) -> None:
        with (
            patch(f"{MODULE}._rez_executable", side_effect=lambda name: name),
            patch(f"{MODULE}.subprocess.run", return_value=_completed(0, stdout="ok\n")) as run,
        ):
            _validate_rez_package("my_lib", "1.0.0")
        assert run.call_args_list[1].args[0] == ["rez", "env", "my_lib-1.0.0", "--", "echo", "ok"]
        text = output.getvalue()
        assert "rez-search my_lib: found" in text
        assert "resolves successfully" in text

    def test_not_found_and_resolve_failure_shows_stderr(self, output: io.StringIO) -> None:
        with (
            patch(f"{MODULE}._rez_executable", side_effect=lambda name: name),
            patch(f"{MODULE}.subprocess.run", return_value=_completed(1, stderr="line1\nPackageNotFound")),
        ):
            _validate_rez_package("my_lib", "1.0.0")
        text = output.getvalue()
        assert "not found" in text
        assert "did not resolve cleanly" in text
        assert "PackageNotFound" in text

    def test_subprocess_errors_are_reported_not_raised(self, output: io.StringIO) -> None:
        with (
            patch(f"{MODULE}._rez_executable", side_effect=lambda name: name),
            patch(f"{MODULE}.subprocess.run", side_effect=OSError("boom")),
        ):
            _validate_rez_package("my_lib", "1.0.0")
        text = output.getvalue()
        assert "rez-search failed" in text
        assert "rez-env resolve failed" in text

    def test_no_version_skips_resolve(self, output: io.StringIO) -> None:
        with (
            patch(f"{MODULE}._rez_executable", side_effect=lambda name: name),
            patch(f"{MODULE}.subprocess.run", return_value=_completed(0)) as run,
        ):
            _validate_rez_package("my_lib", None)
        assert run.call_count == 1
        assert "rez-env" not in output.getvalue()


# ---------------------------------------------------------------------------
# Library package building
# ---------------------------------------------------------------------------


class TestBuildLibraryFromDir:
    @pytest.mark.usefixtures("output", "clean_env")
    def test_passes_manifest_deps_and_flags(self, tmp_path: Path) -> None:
        library_dir = _make_library(
            tmp_path,
            {
                "name": "My Library",
                "metadata": {
                    "dependencies": {
                        "pip_dependencies": ["requests"],
                        "pip_dependencies_exec": ["torch"],
                        "pip_install_flags": ["--torch-backend=auto"],
                    }
                },
            },
        )
        store = tmp_path / "store"
        with (
            patch(f"{MODULE}.install_library_as_rez_package") as install,
            patch(f"{MODULE}.get_library_rez_package_version", return_value="1.0.0") as get_version,
            patch(f"{MODULE}._validate_rez_package") as validate,
        ):
            _build_library_from_dir(library_dir, store, skip_installed=False)

            # The store root is passed explicitly; the process environment is left untouched.
            assert "GTN_REZ_LOCAL_PACKAGES_PATH" not in os.environ

        install.assert_called_once()
        args, kwargs = install.call_args
        assert args == ("My Library", ["requests"])
        assert kwargs["pip_dependencies_exec"] == ["torch"]
        assert kwargs["pip_install_flags"] == ["--torch-backend=auto"]
        assert kwargs["library_file_path"] == library_dir / "griptape_nodes_library.json"
        assert kwargs["skip_installed"] is False
        assert kwargs["packages_root"] == store
        assert get_version.call_args.kwargs["packages_root"] == store
        validate.assert_called_once_with("my_library", "1.0.0")

    @pytest.mark.usefixtures("clean_env")
    def test_no_exec_deps_or_flags_pass_none(self, tmp_path: Path, output: io.StringIO) -> None:
        library_dir = _make_library(tmp_path, {"name": "Plain", "metadata": {"dependencies": {}}})
        with (
            patch(f"{MODULE}.install_library_as_rez_package") as install,
            patch(f"{MODULE}.get_library_rez_package_version", return_value=None),
            patch(f"{MODULE}._validate_rez_package"),
        ):
            _build_library_from_dir(library_dir, tmp_path / "store", skip_installed=True)
        assert install.call_args.kwargs["pip_dependencies_exec"] is None
        assert install.call_args.kwargs["pip_install_flags"] is None
        assert "No pip dependencies found" in output.getvalue()

    @pytest.mark.usefixtures("output", "clean_env")
    def test_no_manifest_exits(self, tmp_path: Path) -> None:
        with pytest.raises(typer.Exit):
            _build_library_from_dir(tmp_path, tmp_path / "store", skip_installed=True)

    @pytest.mark.usefixtures("output", "clean_env")
    def test_manifest_without_name_exits(self, tmp_path: Path) -> None:
        library_dir = _make_library(tmp_path, {"metadata": {}})
        with pytest.raises(typer.Exit):
            _build_library_from_dir(library_dir, tmp_path / "store", skip_installed=True)

    @pytest.mark.usefixtures("clean_env")
    def test_resolve_failure_shows_uv_error_and_exits(self, tmp_path: Path, output: io.StringIO) -> None:
        library_dir = _make_library(tmp_path, {"name": "Broken"})
        error = subprocess.CalledProcessError(1, "uv", stderr="Because torch==99 was not found")
        with (
            patch(f"{MODULE}.install_library_as_rez_package", side_effect=error),
            pytest.raises(typer.Exit),
        ):
            _build_library_from_dir(library_dir, tmp_path / "store", skip_installed=True)
        text = output.getvalue()
        assert "Attempted to build a rez package for 'Broken'" in text
        assert "Because torch==99 was not found" in text

    def test_filesystem_failure_exits(self, tmp_path: Path, output: io.StringIO) -> None:
        library_dir = _make_library(tmp_path, {"name": "Broken"})
        with (
            patch(f"{MODULE}.install_library_as_rez_package", side_effect=OSError("disk full")),
            pytest.raises(typer.Exit),
        ):
            _build_library_from_dir(library_dir, tmp_path / "store", skip_installed=True)
        assert "Attempted to build a rez package for 'Broken'. Failed due to: disk full" in output.getvalue()

    @pytest.mark.usefixtures("output", "clean_env")
    def test_builds_library_dependencies_first(self, tmp_path: Path) -> None:
        library_dir = _make_library(
            tmp_path,
            {
                "name": "Needs Deps",
                "metadata": {
                    "declarations": [{"type": "library_dependency", "url": "https://example.com/dep.git"}],
                },
            },
        )
        calls: list[str] = []
        with (
            patch(f"{MODULE}._build_library_from_git", side_effect=lambda url, *_a, **_k: calls.append(url)),
            patch(f"{MODULE}.install_library_as_rez_package", side_effect=lambda *_a, **_k: calls.append("self")),
            patch(f"{MODULE}.get_library_rez_package_version", return_value="1.0.0"),
            patch(f"{MODULE}._validate_rez_package"),
        ):
            _build_library_from_dir(library_dir, tmp_path / "store", skip_installed=True)
        assert calls == ["https://example.com/dep.git", "self"]

    @pytest.mark.usefixtures("clean_env")
    def test_optional_dependency_failure_continues(self, tmp_path: Path, output: io.StringIO) -> None:
        library_dir = _make_library(
            tmp_path,
            {
                "name": "Optional Deps",
                "metadata": {
                    "declarations": [
                        {"type": "library_dependency", "url": "https://example.com/opt.git", "required": False}
                    ],
                },
            },
        )
        with (
            patch(f"{MODULE}._build_library_from_git", side_effect=typer.Exit(1)),
            patch(f"{MODULE}.install_library_as_rez_package") as install,
            patch(f"{MODULE}.get_library_rez_package_version", return_value="1.0.0"),
            patch(f"{MODULE}._validate_rez_package"),
        ):
            _build_library_from_dir(library_dir, tmp_path / "store", skip_installed=True)
        install.assert_called_once()
        assert "Optional library dependency failed" in output.getvalue()

    @pytest.mark.usefixtures("clean_env")
    def test_required_dependency_failure_stops(self, tmp_path: Path, output: io.StringIO) -> None:
        library_dir = _make_library(
            tmp_path,
            {
                "name": "Required Deps",
                "metadata": {
                    "declarations": [
                        {"type": "library_dependency", "url": "https://example.com/req.git", "required": True}
                    ],
                },
            },
        )
        with (
            patch(f"{MODULE}._build_library_from_git", side_effect=typer.Exit(1)),
            patch(f"{MODULE}.install_library_as_rez_package") as install,
            pytest.raises(typer.Exit),
        ):
            _build_library_from_dir(library_dir, tmp_path / "store", skip_installed=True)
        install.assert_not_called()
        assert "Required library dependency failed" in output.getvalue()


class TestBuildLibrarySources:
    @pytest.mark.usefixtures("output")
    def test_git_clones_then_builds_and_cleans_up(self, tmp_path: Path) -> None:
        cloned: list[Path] = []

        def fake_clone(url: str, target: Path, branch: str | None) -> None:
            assert url == "https://example.com/lib.git"
            assert branch == "main"
            target.mkdir(parents=True)
            cloned.append(target)

        with (
            patch(f"{MODULE}.clone_repository", side_effect=fake_clone),
            patch(f"{MODULE}._build_library_from_dir") as build,
        ):
            _build_library_from_git("https://example.com/lib.git", "main", tmp_path, skip_installed=False)

        build.assert_called_once_with(cloned[0], tmp_path, skip_installed=False)
        assert not cloned[0].parent.exists()

    @pytest.mark.usefixtures("output")
    def test_git_cleans_up_on_failure(self, tmp_path: Path) -> None:
        targets: list[Path] = []

        def fake_clone(_url: str, target: Path, _branch: str | None) -> None:
            target.mkdir(parents=True)
            targets.append(target)

        with (
            patch(f"{MODULE}.clone_repository", side_effect=fake_clone),
            patch(f"{MODULE}._build_library_from_dir", side_effect=typer.Exit(1)),
            pytest.raises(typer.Exit),
        ):
            _build_library_from_git("https://example.com/lib.git", None, tmp_path, skip_installed=True)
        assert not targets[0].parent.exists()

    @pytest.mark.usefixtures("output")
    def test_local_builds_from_directory(self, tmp_path: Path) -> None:
        with patch(f"{MODULE}._build_library_from_dir") as build:
            _build_library_from_local(str(tmp_path), tmp_path / "store", skip_installed=True)
        build.assert_called_once_with(tmp_path, tmp_path / "store", skip_installed=True)

    @pytest.mark.usefixtures("output")
    def test_local_missing_directory_exits(self, tmp_path: Path) -> None:
        with pytest.raises(typer.Exit):
            _build_library_from_local(str(tmp_path / "missing"), tmp_path, skip_installed=True)


# ---------------------------------------------------------------------------
# Command-level tests via CliRunner
# ---------------------------------------------------------------------------


class TestWarnIfRezDoesNotSearch:
    def test_warns_with_the_store_and_the_config_to_add(
        self, tmp_path: Path, output: io.StringIO, rez_searches_every_store: MagicMock
    ) -> None:
        store = tmp_path / "store" / "local"
        rez_searches_every_store.return_value = [store]

        _warn_if_rez_does_not_search(store)

        rez_searches_every_store.assert_called_once_with([store])
        text = output.getvalue()
        assert "Rez does not search" in text
        assert f'packages_path = ModifyList(append=["{store.as_posix()}"])' in text
        assert "never changes your rez configuration" in text

    def test_silent_when_rez_searches_the_store(self, tmp_path: Path, output: io.StringIO) -> None:
        _warn_if_rez_does_not_search(tmp_path / "store" / "local")
        assert output.getvalue() == ""


class TestRezCommandsUseTheRezEnvironment:
    @pytest.mark.usefixtures("output")
    def test_binding_check_runs_with_opt_in_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config = tmp_path / "griptape_rezconfig.py"
        monkeypatch.setenv("GTN_REZ_CONFIG_FILE", str(config))
        monkeypatch.delenv("REZ_CONFIG_FILE", raising=False)
        with (
            patch(f"{MODULE}._rez_executable", return_value="rez-search"),
            patch(f"{MODULE}.subprocess.run", return_value=_completed(0)) as run,
        ):
            _check_rez_bindings()
        assert run.call_args.kwargs["env"]["REZ_CONFIG_FILE"] == str(config)

    @pytest.mark.usefixtures("output")
    def test_validation_runs_with_opt_in_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config = tmp_path / "griptape_rezconfig.py"
        monkeypatch.setenv("GTN_REZ_CONFIG_FILE", str(config))
        monkeypatch.delenv("REZ_CONFIG_FILE", raising=False)
        with (
            patch(f"{MODULE}._rez_executable", side_effect=lambda name: name),
            patch(f"{MODULE}.subprocess.run", return_value=_completed(0, stdout="ok")) as run,
        ):
            _validate_rez_package("my_lib", "1.0.0")
        assert run.call_count == 2  # noqa: PLR2004 -- rez-search, then rez env
        for call in run.call_args_list:
            assert call.kwargs["env"]["REZ_CONFIG_FILE"] == str(config)


class TestBuildLibraryPackageCommand:
    @pytest.mark.usefixtures("output")
    def test_rejects_both_sources(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app, ["build-library-package", "--git-url", "https://x/y.git", "--local-path", str(tmp_path)]
        )
        assert result.exit_code == 1

    @pytest.mark.usefixtures("output")
    def test_rejects_no_source(self) -> None:
        result = runner.invoke(app, ["build-library-package"])
        assert result.exit_code == 1

    @pytest.mark.usefixtures("clean_env")
    def test_requires_packages_path_or_env(self, tmp_path: Path, output: io.StringIO) -> None:
        result = runner.invoke(app, ["build-library-package", "--local-path", str(tmp_path)])
        assert result.exit_code == 1
        assert "GTN_REZ_LOCAL_PACKAGES_PATH" in output.getvalue()

    @pytest.mark.usefixtures("output", "clean_env")
    def test_packages_path_falls_back_to_env(self, tmp_path: Path) -> None:
        os.environ["GTN_REZ_LOCAL_PACKAGES_PATH"] = str(tmp_path / "store" / "local")
        with (
            patch(f"{MODULE}._check_rez_bindings") as check,
            patch(f"{MODULE}._build_library_from_local") as build,
        ):
            result = runner.invoke(app, ["build-library-package", "--local-path", str(tmp_path), "--reinstall"])
        assert result.exit_code == 0, result.output
        check.assert_called_once_with()
        build.assert_called_once_with(str(tmp_path), tmp_path / "store", skip_installed=False)

    @pytest.mark.usefixtures("output")
    def test_checks_that_rez_searches_the_store_it_builds_into(self, tmp_path: Path) -> None:
        with (
            patch(f"{MODULE}._warn_if_rez_does_not_search") as warn,
            patch(f"{MODULE}._check_rez_bindings"),
            patch(f"{MODULE}._build_library_from_local"),
        ):
            result = runner.invoke(
                app, ["build-library-package", "--local-path", str(tmp_path), "--packages-path", str(tmp_path)]
            )
        assert result.exit_code == 0, result.output
        warn.assert_called_once_with(tmp_path / "local")

    @pytest.mark.usefixtures("output")
    def test_git_source_uses_explicit_packages_path(self, tmp_path: Path) -> None:
        with (
            patch(f"{MODULE}._check_rez_bindings"),
            patch(f"{MODULE}._build_library_from_git") as build,
        ):
            result = runner.invoke(
                app,
                [
                    "build-library-package",
                    "--git-url",
                    "https://example.com/lib.git",
                    "--branch",
                    "v1",
                    "--packages-path",
                    str(tmp_path),
                ],
            )
        assert result.exit_code == 0, result.output
        build.assert_called_once_with("https://example.com/lib.git", "v1", tmp_path, skip_installed=True)

    @pytest.mark.usefixtures("output")
    def test_missing_bindings_abort_before_building(self, tmp_path: Path) -> None:
        with (
            patch(f"{MODULE}._rez_executable", return_value="rez-search"),
            patch(f"{MODULE}.subprocess.run", return_value=_completed(1)),
            patch(f"{MODULE}.typer.confirm", return_value=False),
            patch(f"{MODULE}._build_library_from_local") as build,
        ):
            result = runner.invoke(
                app, ["build-library-package", "--local-path", str(tmp_path), "--packages-path", str(tmp_path)]
            )
        assert result.exit_code != 0
        build.assert_not_called()


class TestBuildEnginePackageCommand:
    @pytest.mark.usefixtures("output", "clean_env")
    def test_yes_mode_builds_without_prompting(self, tmp_path: Path) -> None:
        repo = _make_engine_repo(tmp_path, deps=["requests>=2"])
        store = tmp_path / "store"
        with (
            patch(f"{MODULE}._check_rez_bindings") as check,
            patch(f"{MODULE}.typer.confirm") as confirm,
            patch(f"{MODULE}.rez_uv_install") as install,
            patch(f"{MODULE}.build_direct_requires", return_value=["requests-2.32.3"]),
            patch(f"{MODULE}._validate_rez_package") as validate,
        ):
            result = runner.invoke(
                app,
                ["build-engine-package", "--engine-repo", str(repo), "--packages-path", str(store), "--yes"],
            )
        assert result.exit_code == 0, result.output
        check.assert_called_once_with(interactive=False)
        confirm.assert_not_called()
        install.assert_called_once_with(["requests>=2"], packages_dir=store, skip_installed=True)
        validate.assert_called_once_with("griptape_nodes_engine", "1.2.3")
        assert (store / "local" / "griptape_nodes_engine" / "1.2.3" / "package.py").exists()

    @pytest.mark.usefixtures("output", "clean_env")
    def test_checks_that_rez_searches_the_store_it_builds_into(self, tmp_path: Path) -> None:
        repo = _make_engine_repo(tmp_path)
        store = tmp_path / "store"
        with (
            patch(f"{MODULE}._warn_if_rez_does_not_search") as warn,
            patch(f"{MODULE}._check_rez_bindings"),
            patch(f"{MODULE}.rez_uv_install"),
            patch(f"{MODULE}.build_direct_requires", return_value=[]),
            patch(f"{MODULE}._validate_rez_package"),
        ):
            result = runner.invoke(
                app, ["build-engine-package", "--engine-repo", str(repo), "--packages-path", str(store), "--yes"]
            )
        assert result.exit_code == 0, result.output
        warn.assert_called_once_with(store / "local")

    @pytest.mark.usefixtures("clean_env")
    def test_interactive_decline_aborts_before_writing(self, tmp_path: Path, output: io.StringIO) -> None:
        repo = _make_engine_repo(tmp_path)
        os.environ["GTN_REZ_ROOT"] = str(tmp_path / "studio")
        existing = tmp_path / "studio" / "rez" / "packages" / "local" / "griptape_nodes_engine" / "1.2.3"
        existing.mkdir(parents=True)
        with (
            patch(f"{MODULE}._check_rez_bindings") as check,
            patch(f"{MODULE}.typer.confirm", return_value=False),
            patch(f"{MODULE}.rez_uv_install") as install,
        ):
            result = runner.invoke(app, ["build-engine-package", "--engine-repo", str(repo)])
        assert result.exit_code != 0
        check.assert_called_once_with(interactive=True)
        install.assert_not_called()
        assert "will be overwritten" in output.getvalue()

    @pytest.mark.usefixtures("output", "clean_env")
    def test_missing_bindings_abort_in_interactive_mode(self, tmp_path: Path) -> None:
        repo = _make_engine_repo(tmp_path)
        os.environ["GTN_REZ_ROOT"] = str(tmp_path / "studio")
        with (
            patch(f"{MODULE}._rez_executable", return_value="rez-search"),
            patch(f"{MODULE}.subprocess.run", return_value=_completed(1)),
            patch(f"{MODULE}.typer.confirm", return_value=False) as confirm,
            patch(f"{MODULE}.rez_uv_install") as install,
        ):
            result = runner.invoke(app, ["build-engine-package", "--engine-repo", str(repo)])
        assert result.exit_code != 0
        assert confirm.call_args.args[0] == "Continue building without rez bindings?"
        install.assert_not_called()

    @pytest.mark.usefixtures("output", "clean_env")
    def test_yes_mode_without_root_or_path_exits(self, tmp_path: Path) -> None:
        repo = _make_engine_repo(tmp_path)
        result = runner.invoke(app, ["build-engine-package", "--engine-repo", str(repo), "--yes"])
        assert result.exit_code == 1


class TestApp:
    def test_registers_both_commands(self) -> None:
        names = {command.name for command in rez.app.registered_commands}
        assert names == {"build-engine-package", "build-library-package"}
