"""Unit tests for the rez CLI commands."""

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
    BuildTarget,
    LaunchSettings,
    LibraryBuildOptions,
    _admin_path,
    _build_library_from_dir,
    _build_library_from_git,
    _build_library_from_local,
    _can_prompt,
    _check_rez_bindings,
    _copy_dist_info,
    _install_deps,
    _is_absolute_for,
    _launch_package_content,
    _next_launch_version,
    _platform_values,
    _print_library_next_steps,
    _read_dependencies,
    _read_project_metadata,
    _read_version,
    _resolve_and_validate_repo,
    _resolve_build_target,
    _resolve_engine_repo,
    _validate_rez_package,
    _warn_if_config_replaces_search_path,
    _warn_if_rez_does_not_search,
    _write_engine_package,
    app,
)
from griptape_nodes.utils.rez_utils import LibraryInstallResult, RezBase, RezSetup
from griptape_nodes.utils.rez_uv import InstallReport, RezInstallError

if TYPE_CHECKING:
    from collections.abc import Iterator

MODULE = "griptape_nodes.cli.commands.rez"
PYTHON_FAMILY = f"python-{sys.version_info.major}.{sys.version_info.minor}"
TOOLS = Path("/studio/rez/bin")

runner = CliRunner()


def _setup(  # noqa: PLR0913
    *,
    enabled: bool = True,
    reason: str = "",
    local: Path | None = None,
    config: Path | None = None,
    base: Path | None = None,
    warnings: tuple[str, ...] = (),
) -> RezSetup:
    return RezSetup(
        enabled=enabled,
        disabled_reason=reason,
        bin_path=TOOLS if enabled else None,
        base=RezBase(path=base, source="GTN_REZ_ROOT") if base is not None else None,
        local_packages_path=local,
        config_file=config,
        warnings=warnings,
    )


@pytest.fixture(autouse=True)
def rez_searches_every_store() -> Iterator[MagicMock]:
    """Default: rez searches every store, so no test depends on a local rez-config binary."""
    with (
        patch(f"{MODULE}.rez_unsearched_stores", return_value=[]) as unsearched,
        patch(f"{MODULE}.rez_config_dropped_paths", return_value=[]),
    ):
        yield unsearched


@pytest.fixture
def rez_on() -> Iterator[MagicMock]:
    """Rez is active; tests set the local store with ``rez_on.return_value = _setup(local=...)``."""
    with patch(f"{MODULE}.rez_setup", return_value=_setup()) as setup:
        yield setup


@pytest.fixture
def output() -> Iterator[io.StringIO]:
    """Route the CLI's rich console into a wide in-memory buffer so text assertions are stable."""
    buffer = io.StringIO()
    with patch(f"{MODULE}.console", Console(file=buffer, width=400, force_terminal=False, color_system=None)):
        yield buffer


@pytest.fixture
def clean_env() -> Iterator[None]:
    """Isolate os.environ."""
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


def _options(store: Path, *, skip_installed: bool = True, interactive: bool = False) -> LibraryBuildOptions:
    return LibraryBuildOptions(store=store, skip_installed=skip_installed, interactive=interactive)


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
# Shared setup: prompting, the build target, and the setup panel
# ---------------------------------------------------------------------------


class TestCanPrompt:
    def test_never_with_yes(self) -> None:
        with patch(f"{MODULE}.sys.stdin") as stdin:
            stdin.isatty.return_value = True
            assert not _can_prompt(yes=True)

    def test_only_in_a_terminal(self) -> None:
        with patch(f"{MODULE}.sys.stdin") as stdin, patch(f"{MODULE}.sys.stdout") as stdout:
            stdout.isatty.return_value = True
            stdin.isatty.return_value = False
            assert not _can_prompt(yes=False)
            stdin.isatty.return_value = True
            assert _can_prompt(yes=False)

    def test_windows_null_stdin_with_piped_output_does_not_prompt(self) -> None:
        # On Windows NUL reports isatty() True; a script capturing output is still not a terminal.
        with patch(f"{MODULE}.sys.stdin") as stdin, patch(f"{MODULE}.sys.stdout") as stdout:
            stdin.isatty.return_value = True
            stdout.isatty.return_value = False
            assert not _can_prompt(yes=False)


class TestResolveBuildTarget:
    def test_flag_wins(self, tmp_path: Path) -> None:
        target = _resolve_build_target(str(tmp_path / "flag"), _setup(local=tmp_path / "env"), interactive=False)
        assert target == BuildTarget(store=tmp_path / "flag", source="--local-packages-path")

    def test_env_used_without_flag(self, tmp_path: Path) -> None:
        target = _resolve_build_target(None, _setup(local=tmp_path / "env"), interactive=False)
        assert target == BuildTarget(store=tmp_path / "env", source="GTN_REZ_LOCAL_PACKAGES_PATH")

    def test_missing_store_stops_without_prompting(self, output: io.StringIO) -> None:
        with patch(f"{MODULE}.typer.prompt") as prompt, pytest.raises(typer.Exit):
            _resolve_build_target(None, _setup(), interactive=False)
        prompt.assert_not_called()
        text = output.getvalue()
        assert "GTN_REZ_LOCAL_PACKAGES_PATH" in text
        assert "never builds into your studio's release packages" in text

    def test_missing_store_is_asked_for_interactively(self, tmp_path: Path, output: io.StringIO) -> None:
        with patch(f"{MODULE}.typer.prompt", return_value=str(tmp_path / "typed")):
            target = _resolve_build_target(None, _setup(), interactive=True)
        assert target == BuildTarget(store=tmp_path / "typed", source="entered")
        assert f"GTN_REZ_LOCAL_PACKAGES_PATH={tmp_path / 'typed'}" in output.getvalue()


class TestPrepareBuild:
    def test_rez_off_prints_reason_and_stops(self, output: io.StringIO) -> None:
        reason = "Rez is not active: no rez-env was found in /x (GTN_REZ_BIN_PATH)."
        with patch(f"{MODULE}.rez_setup", return_value=_setup(enabled=False, reason=reason)), pytest.raises(typer.Exit):
            rez._prepare_build(None, interactive=False)
        text = output.getvalue()
        assert reason in text
        assert "Rez setup" in text

    def test_rez_not_configured_says_which_variable(self, output: io.StringIO) -> None:
        with patch(f"{MODULE}.rez_setup", return_value=_setup(enabled=False)), pytest.raises(typer.Exit):
            rez._prepare_build(None, interactive=False)
        assert "GTN_REZ_BIN_PATH is not set" in output.getvalue()

    def test_reports_setup_and_runs_checks(self, tmp_path: Path, output: io.StringIO) -> None:
        setup = _setup(
            local=tmp_path / "local",
            config=tmp_path / "ours.py",
            base=tmp_path,
            warnings=("X is ignored",),
        )
        with (
            patch(f"{MODULE}.rez_setup", return_value=setup),
            patch(f"{MODULE}._warn_if_rez_does_not_search") as unsearched,
            patch(f"{MODULE}._warn_if_config_replaces_search_path") as replaced,
            patch(f"{MODULE}._check_rez_bindings") as bindings,
        ):
            target = rez._prepare_build(None, interactive=False)

        assert target.store == tmp_path / "local"
        unsearched.assert_called_once_with(tmp_path / "local")
        replaced.assert_called_once_with()
        bindings.assert_called_once_with(interactive=False)
        text = output.getvalue()
        assert str(TOOLS) in text
        assert "rez-env found" in text
        assert "GTN_REZ_ROOT" in text
        assert "added after yours" in text
        assert "X is ignored" in text

    def test_panel_shows_missing_rez_env(self, output: io.StringIO) -> None:
        setup = RezSetup(
            enabled=False,
            disabled_reason="Rez is not active: no rez-env",
            bin_path=Path("/nowhere"),
            base=None,
            local_packages_path=None,
            config_file=None,
            warnings=(),
        )
        with patch(f"{MODULE}.rez_setup", return_value=setup), pytest.raises(typer.Exit):
            rez._prepare_build(None, interactive=False)
        assert "no rez-env" in output.getvalue()


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

    def test_read_project_metadata(self, tmp_path: Path) -> None:
        path = _write_pyproject(tmp_path, version="3.1.0", deps=["a", "b"])
        assert _read_project_metadata(path) == ("3.1.0", ["a", "b"])

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
        assert "Could not auto-detect the engine repository" in output.getvalue()


class TestResolveAndValidateRepo:
    @pytest.mark.usefixtures("output")
    def test_returns_repo_and_pyproject(self, tmp_path: Path) -> None:
        repo = _make_engine_repo(tmp_path)
        assert _resolve_and_validate_repo(str(repo), interactive=False) == (repo, repo / "pyproject.toml")

    @pytest.mark.usefixtures("output")
    def test_prompts_when_not_found_interactively(self, tmp_path: Path) -> None:
        repo = _make_engine_repo(tmp_path)
        with (
            patch(f"{MODULE}._resolve_engine_repo", return_value=None),
            patch(f"{MODULE}.typer.prompt", return_value=str(repo)),
        ):
            repo_path, pyproject = _resolve_and_validate_repo(None, interactive=True)
        assert pyproject == repo_path / "pyproject.toml"
        assert pyproject.exists()

    def test_not_found_without_terminal_stops(self, output: io.StringIO) -> None:
        with (
            patch(f"{MODULE}._resolve_engine_repo", return_value=None),
            patch(f"{MODULE}.typer.prompt") as prompt,
            pytest.raises(typer.Exit),
        ):
            _resolve_and_validate_repo(None, interactive=False)
        prompt.assert_not_called()
        assert "--engine-repo" in output.getvalue()

    @pytest.mark.usefixtures("output")
    def test_prompted_directory_missing_exits(self, tmp_path: Path) -> None:
        with (
            patch(f"{MODULE}._resolve_engine_repo", return_value=None),
            patch(f"{MODULE}.typer.prompt", return_value=str(tmp_path / "missing")),
            pytest.raises(typer.Exit),
        ):
            _resolve_and_validate_repo(None, interactive=True)

    @pytest.mark.usefixtures("output")
    def test_missing_pyproject_exits(self, tmp_path: Path) -> None:
        with pytest.raises(typer.Exit):
            _resolve_and_validate_repo(str(tmp_path), interactive=False)


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

    def test_partial_install_lists_failures_and_exits(self, tmp_path: Path, output: io.StringIO) -> None:
        error = RezInstallError(["idna==3.7 (download failed: no matching distribution)"])
        with (
            patch(f"{MODULE}.rez_uv_install", side_effect=error),
            pytest.raises(typer.Exit),
        ):
            _install_deps(["requests"], tmp_path, skip_installed=True)
        text = output.getvalue()
        assert "1 package(s) could not be installed" in text
        assert "idna==3.7 (download failed: no matching distribution)" in text

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

        version_dir = store / "griptape_nodes_engine" / "1.2.3"
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

    def test_replaces_existing_version(self, tmp_path: Path, output: io.StringIO) -> None:
        repo = _make_engine_repo(tmp_path)
        store = tmp_path / "store"
        stale = store / "griptape_nodes_engine" / "1.2.3" / "stale.txt"
        stale.parent.mkdir(parents=True)
        stale.write_text("old", encoding="utf-8")
        with patch(f"{MODULE}.build_direct_requires", return_value=[]):
            _write_engine_package(repo, store, "1.2.3", [])
        assert not stale.exists()
        assert "replacing" in output.getvalue()

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


class TestNextSteps:
    def test_library_next_steps_with_and_without_version(self, output: io.StringIO) -> None:
        _print_library_next_steps("my_lib", "1.0.0")
        _print_library_next_steps("other_lib", None)
        text = output.getvalue()
        assert "REZ:my_lib-1.0.0" in text
        assert '"REZ:other_lib"' in text
        assert "releasing-packages.md" in text


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
    @pytest.fixture(autouse=True)
    def _no_git(self) -> Iterator[None]:
        with patch("griptape_nodes.utils.rez_utils.get_git_repository_root", return_value=None):
            yield

    @pytest.mark.usefixtures("clean_env")
    def test_passes_manifest_deps_and_flags(self, tmp_path: Path, output: io.StringIO) -> None:
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
            _build_library_from_dir(library_dir, _options(store, skip_installed=False))

            # The store is passed explicitly; the process environment is left untouched.
            assert "GTN_REZ_LOCAL_PACKAGES_PATH" not in os.environ

        install.assert_called_once()
        args, kwargs = install.call_args
        assert args == ("My Library", ["requests"])
        assert kwargs["pip_dependencies_exec"] == ["torch"]
        assert kwargs["pip_install_flags"] == ["--torch-backend=auto"]
        assert kwargs["library_file_path"] == library_dir / "griptape_nodes_library.json"
        assert kwargs["skip_installed"] is False
        assert kwargs["store"] == store
        assert get_version.call_args.kwargs["store"] == store
        validate.assert_called_once_with("my_library", "1.0.0")
        text = output.getvalue()
        assert "my_library-1.0.0 (named after folder 'my_library')" in text
        assert "1 edit + 1 exec" in text
        assert "--torch-backend=auto" in text

    @pytest.mark.usefixtures("clean_env")
    def test_no_exec_deps_or_flags_pass_none(self, tmp_path: Path, output: io.StringIO) -> None:
        library_dir = _make_library(tmp_path, {"name": "Plain", "metadata": {"dependencies": {}}})
        with (
            patch(f"{MODULE}.install_library_as_rez_package") as install,
            patch(f"{MODULE}.get_library_rez_package_version", return_value=None),
            patch(f"{MODULE}._validate_rez_package"),
        ):
            _build_library_from_dir(library_dir, _options(tmp_path / "store"))
        assert install.call_args.kwargs["pip_dependencies_exec"] is None
        assert install.call_args.kwargs["pip_install_flags"] is None
        assert "No pip dependencies found" in output.getvalue()

    @pytest.mark.usefixtures("output", "clean_env")
    def test_interactive_decline_builds_nothing(self, tmp_path: Path) -> None:
        library_dir = _make_library(tmp_path, {"name": "Plain"})
        with (
            patch(f"{MODULE}.typer.confirm", return_value=False),
            patch(f"{MODULE}.install_library_as_rez_package") as install,
            pytest.raises(typer.Abort),
        ):
            _build_library_from_dir(library_dir, _options(tmp_path / "store", interactive=True))
        install.assert_not_called()

    @pytest.mark.usefixtures("output", "clean_env")
    def test_no_manifest_exits(self, tmp_path: Path) -> None:
        with pytest.raises(typer.Exit):
            _build_library_from_dir(tmp_path, _options(tmp_path / "store"))

    @pytest.mark.usefixtures("output", "clean_env")
    def test_manifest_without_name_exits(self, tmp_path: Path) -> None:
        library_dir = _make_library(tmp_path, {"metadata": {}})
        with pytest.raises(typer.Exit):
            _build_library_from_dir(library_dir, _options(tmp_path / "store"))

    @pytest.mark.usefixtures("clean_env")
    def test_resolve_failure_shows_uv_error_and_exits(self, tmp_path: Path, output: io.StringIO) -> None:
        library_dir = _make_library(tmp_path, {"name": "Broken"})
        error = subprocess.CalledProcessError(1, "uv", stderr="Because torch==99 was not found")
        with (
            patch(f"{MODULE}.install_library_as_rez_package", side_effect=error),
            pytest.raises(typer.Exit),
        ):
            _build_library_from_dir(library_dir, _options(tmp_path / "store"))
        text = output.getvalue()
        assert "Attempted to build a rez package for 'Broken'" in text
        assert "Because torch==99 was not found" in text

    def test_partial_install_lists_failures_and_exits(self, tmp_path: Path, output: io.StringIO) -> None:
        library_dir = _make_library(tmp_path, {"name": "Broken"})
        error = RezInstallError(["torch==2.7.0 (download failed: no matching distribution)"])
        with (
            patch(f"{MODULE}.install_library_as_rez_package", side_effect=error),
            patch(f"{MODULE}._validate_rez_package") as validate,
            pytest.raises(typer.Exit),
        ):
            _build_library_from_dir(library_dir, _options(tmp_path / "store"))
        text = output.getvalue()
        assert "Attempted to build a rez package for 'Broken'" in text
        assert "torch==2.7.0 (download failed: no matching distribution)" in text
        validate.assert_not_called()

    def test_filesystem_failure_exits(self, tmp_path: Path, output: io.StringIO) -> None:
        library_dir = _make_library(tmp_path, {"name": "Broken"})
        with (
            patch(f"{MODULE}.install_library_as_rez_package", side_effect=OSError("disk full")),
            pytest.raises(typer.Exit),
        ):
            _build_library_from_dir(library_dir, _options(tmp_path / "store"))
        assert "Attempted to build a rez package for 'Broken'. Failed due to: disk full" in output.getvalue()

    @pytest.mark.usefixtures("output", "clean_env")
    def test_builds_library_dependencies_first_without_questions(self, tmp_path: Path) -> None:
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
        dependency_options: list[LibraryBuildOptions] = []

        def fake_git(url: str, _branch: str | None, options: LibraryBuildOptions) -> None:
            calls.append(url)
            dependency_options.append(options)

        with (
            patch(f"{MODULE}.typer.confirm", return_value=True),
            patch(f"{MODULE}._build_library_from_git", side_effect=fake_git),
            patch(f"{MODULE}.install_library_as_rez_package", side_effect=lambda *_a, **_k: calls.append("self")),
            patch(f"{MODULE}.get_library_rez_package_version", return_value="1.0.0"),
            patch(f"{MODULE}._validate_rez_package"),
        ):
            _build_library_from_dir(library_dir, _options(tmp_path / "store", skip_installed=False, interactive=True))
        assert calls == ["https://example.com/dep.git", "self"]
        assert dependency_options == [_options(tmp_path / "store", skip_installed=True, interactive=False)]

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
            _build_library_from_dir(library_dir, _options(tmp_path / "store"))
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
            _build_library_from_dir(library_dir, _options(tmp_path / "store"))
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

        options = _options(tmp_path, skip_installed=False)
        with (
            patch(f"{MODULE}.clone_repository", side_effect=fake_clone),
            patch(f"{MODULE}._build_library_from_dir") as build,
        ):
            _build_library_from_git("https://example.com/lib.git", "main", options)

        build.assert_called_once_with(cloned[0], options)
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
            _build_library_from_git("https://example.com/lib.git", None, _options(tmp_path))
        assert not targets[0].parent.exists()

    @pytest.mark.usefixtures("output")
    def test_local_builds_from_directory(self, tmp_path: Path) -> None:
        options = _options(tmp_path / "store")
        with patch(f"{MODULE}._build_library_from_dir") as build:
            _build_library_from_local(str(tmp_path), options)
        build.assert_called_once_with(tmp_path, options)

    @pytest.mark.usefixtures("output")
    def test_local_missing_directory_exits(self, tmp_path: Path) -> None:
        with pytest.raises(typer.Exit):
            _build_library_from_local(str(tmp_path / "missing"), _options(tmp_path))


# ---------------------------------------------------------------------------
# Search-path warnings and the rez environment
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


class TestWarnIfConfigReplacesSearchPath:
    def test_lists_dropped_studio_paths(self, output: io.StringIO) -> None:
        with patch(f"{MODULE}.rez_config_dropped_paths", return_value=[Path("/studio/packages")]):
            _warn_if_config_replaces_search_path()
        text = output.getvalue()
        assert "replaces your studio's package search path" in text
        assert str(Path("/studio/packages")) in text
        assert "ModifyList(append=[...])" in text

    def test_silent_when_config_extends(self, output: io.StringIO) -> None:
        _warn_if_config_replaces_search_path()
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


# ---------------------------------------------------------------------------
# Command-level tests via CliRunner (not a terminal, so nothing prompts)
# ---------------------------------------------------------------------------


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

    def test_rez_off_stops_with_reason(self, tmp_path: Path, output: io.StringIO) -> None:
        reason = "Rez is not active: GTN_REZ_ROOT is set, but GTN_REZ_BIN_PATH is not."
        with (
            patch(f"{MODULE}.rez_setup", return_value=_setup(enabled=False, reason=reason)),
            patch(f"{MODULE}._build_library_from_local") as build,
        ):
            result = runner.invoke(app, ["build-library-package", "--local-path", str(tmp_path)])
        assert result.exit_code == 1
        assert reason in output.getvalue()
        build.assert_not_called()

    @pytest.mark.usefixtures("rez_on")
    def test_requires_a_store(self, tmp_path: Path, output: io.StringIO) -> None:
        result = runner.invoke(app, ["build-library-package", "--local-path", str(tmp_path)])
        assert result.exit_code == 1
        assert "GTN_REZ_LOCAL_PACKAGES_PATH" in output.getvalue()

    @pytest.mark.usefixtures("output")
    def test_store_from_env_and_rebuild(self, tmp_path: Path, rez_on: MagicMock) -> None:
        store = tmp_path / "store"
        rez_on.return_value = _setup(local=store)
        with (
            patch(f"{MODULE}._check_rez_bindings") as check,
            patch(f"{MODULE}._build_library_from_local") as build,
        ):
            result = runner.invoke(app, ["build-library-package", "--local-path", str(tmp_path), "--rebuild"])
        assert result.exit_code == 0, result.output
        check.assert_called_once_with(interactive=False)
        build.assert_called_once_with(str(tmp_path), _options(store, skip_installed=False))

    @pytest.mark.usefixtures("output", "rez_on")
    def test_checks_that_rez_searches_the_store_it_builds_into(self, tmp_path: Path) -> None:
        with (
            patch(f"{MODULE}._warn_if_rez_does_not_search") as warn,
            patch(f"{MODULE}._check_rez_bindings"),
            patch(f"{MODULE}._build_library_from_local"),
        ):
            result = runner.invoke(
                app, ["build-library-package", "--local-path", str(tmp_path), "--local-packages-path", str(tmp_path)]
            )
        assert result.exit_code == 0, result.output
        warn.assert_called_once_with(tmp_path)

    @pytest.mark.usefixtures("output", "rez_on")
    def test_git_source_uses_explicit_store(self, tmp_path: Path) -> None:
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
                    "--local-packages-path",
                    str(tmp_path),
                ],
            )
        assert result.exit_code == 0, result.output
        build.assert_called_once_with("https://example.com/lib.git", "v1", _options(tmp_path))

    @pytest.mark.usefixtures("output", "rez_on")
    def test_missing_bindings_abort_before_building_interactively(self, tmp_path: Path) -> None:
        with (
            patch(f"{MODULE}._can_prompt", return_value=True),
            patch(f"{MODULE}._rez_executable", return_value="rez-search"),
            patch(f"{MODULE}.subprocess.run", return_value=_completed(1)),
            patch(f"{MODULE}.typer.confirm", return_value=False),
            patch(f"{MODULE}._build_library_from_local") as build,
        ):
            result = runner.invoke(
                app, ["build-library-package", "--local-path", str(tmp_path), "--local-packages-path", str(tmp_path)]
            )
        assert result.exit_code != 0
        build.assert_not_called()


class TestBuildEnginePackageCommand:
    @pytest.mark.usefixtures("output", "rez_on")
    def test_builds_without_prompting(self, tmp_path: Path) -> None:
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
                ["build-engine-package", "--engine-repo", str(repo), "--local-packages-path", str(store), "--yes"],
            )
        assert result.exit_code == 0, result.output
        check.assert_called_once_with(interactive=False)
        confirm.assert_not_called()
        install.assert_called_once_with(["requests>=2"], packages_dir=store, skip_installed=True)
        validate.assert_called_once_with("griptape_nodes_engine", "1.2.3")
        assert (store / "griptape_nodes_engine" / "1.2.3" / "package.py").exists()

    @pytest.mark.usefixtures("output", "rez_on")
    def test_rebuild_reinstalls_dependencies(self, tmp_path: Path) -> None:
        repo = _make_engine_repo(tmp_path, deps=["requests>=2"])
        store = tmp_path / "store"
        with (
            patch(f"{MODULE}._check_rez_bindings"),
            patch(f"{MODULE}.rez_uv_install") as install,
            patch(f"{MODULE}.build_direct_requires", return_value=[]),
            patch(f"{MODULE}._validate_rez_package"),
        ):
            result = runner.invoke(
                app,
                ["build-engine-package", "--engine-repo", str(repo), "--local-packages-path", str(store), "--rebuild"],
            )
        assert result.exit_code == 0, result.output
        assert install.call_args.kwargs["skip_installed"] is False

    @pytest.mark.usefixtures("output")
    def test_checks_that_rez_searches_the_store_it_builds_into(self, tmp_path: Path, rez_on: MagicMock) -> None:
        repo = _make_engine_repo(tmp_path)
        store = tmp_path / "store"
        rez_on.return_value = _setup(local=store)
        with (
            patch(f"{MODULE}._warn_if_rez_does_not_search") as warn,
            patch(f"{MODULE}._check_rez_bindings"),
            patch(f"{MODULE}.rez_uv_install"),
            patch(f"{MODULE}.build_direct_requires", return_value=[]),
            patch(f"{MODULE}._validate_rez_package"),
        ):
            result = runner.invoke(app, ["build-engine-package", "--engine-repo", str(repo)])
        assert result.exit_code == 0, result.output
        warn.assert_called_once_with(store)

    def test_interactive_decline_aborts_before_writing(
        self, tmp_path: Path, output: io.StringIO, rez_on: MagicMock
    ) -> None:
        repo = _make_engine_repo(tmp_path)
        store = tmp_path / "store"
        (store / "griptape_nodes_engine" / "1.2.3").mkdir(parents=True)
        rez_on.return_value = _setup(local=store)
        with (
            patch(f"{MODULE}._can_prompt", return_value=True),
            patch(f"{MODULE}._check_rez_bindings") as check,
            patch(f"{MODULE}.typer.confirm", return_value=False),
            patch(f"{MODULE}.rez_uv_install") as install,
        ):
            result = runner.invoke(app, ["build-engine-package", "--engine-repo", str(repo)])
        assert result.exit_code != 0
        check.assert_called_once_with(interactive=True)
        install.assert_not_called()
        assert "will be replaced" in output.getvalue()

    @pytest.mark.usefixtures("output", "rez_on")
    def test_without_store_exits(self, tmp_path: Path) -> None:
        repo = _make_engine_repo(tmp_path)
        result = runner.invoke(app, ["build-engine-package", "--engine-repo", str(repo), "--yes"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# write-launch-package
# ---------------------------------------------------------------------------


class _Rex:
    """Just enough of rez's commands() namespace to run a generated package."""

    class Stop(Exception):  # noqa: N818 -- mirrors rez's stop()
        pass

    def __init__(self, platform: str) -> None:
        self.system = type("System", (), {"platform": platform})()
        self.env: dict[str, str] = {}

    def run(self, content: str) -> dict[str, str]:
        namespace: dict = {}
        exec(compile(content, "package.py", "exec"), namespace)  # noqa: S102
        commands = namespace["commands"]
        commands.__globals__.update(system=self.system, setenv=self.env.__setitem__, stop=self._stop)
        commands()
        return self.env

    def _stop(self, message: str) -> None:
        raise self.Stop(message)


def _settings(**kwargs: object) -> LaunchSettings:
    defaults: dict = {
        "per_platform": {"osx": {"GTN_REZ_BIN_PATH": "/Volumes/p/rez/bin"}},
        "shared": {},
        "engine_version": "0.103.0",
    }
    defaults.update(kwargs)
    return LaunchSettings(**defaults)


class TestLaunchPackageContent:
    def test_values_are_chosen_per_platform(self) -> None:
        settings = _settings(
            per_platform={
                "osx": {"GTN_REZ_BIN_PATH": "/Volumes/p/rez/bin"},
                "windows": {"GTN_REZ_BIN_PATH": "P:/rez/bin", "GTN_REZ_LOCAL_PACKAGES_PATH": "P:/rez/local"},
            },
            shared={"GTN_REZ_PATH_MAP": "osx=/Volumes/p;windows=P:"},
        )
        content = _launch_package_content(settings, "1.2.0")

        assert "name = 'griptape_launch'" in content
        assert "version = '1.2.0'" in content
        assert "requires = ['griptape_nodes_engine-0.103.0']" in content
        assert _Rex("windows").run(content) == {
            "GTN_REZ_BIN_PATH": "P:/rez/bin",
            "GTN_REZ_LOCAL_PACKAGES_PATH": "P:/rez/local",
            "GTN_REZ_PATH_MAP": "osx=/Volumes/p;windows=P:",
        }
        assert _Rex("osx").run(content)["GTN_REZ_BIN_PATH"] == "/Volumes/p/rez/bin"

    def test_unconfigured_platform_stops_with_a_message(self) -> None:
        content = _launch_package_content(_settings(), "1.0.0")
        with pytest.raises(_Rex.Stop, match="no rez tools folder for platform linux"):
            _Rex("linux").run(content)

    def test_uses_the_variable_names_the_engine_reads(self) -> None:
        content = _launch_package_content(_settings(), "1.0.0")
        assert "'GTN_REZ_BIN_PATH'" in content
        assert "GTN_REZ_BIN'" not in content


class TestPlatformValues:
    def test_platform_prefixed_and_bare_values(self) -> None:
        with patch(f"{MODULE}.current_platform_key", return_value="osx"):
            values = _platform_values(["windows=P:/rez/bin", "/Volumes/p/rez/bin"], "--tools")
        assert values == {"windows": "P:/rez/bin", "osx": "/Volumes/p/rez/bin"}

    def test_unknown_platform_stops(self, output: io.StringIO) -> None:
        with pytest.raises(typer.Exit):
            _platform_values(["amiga=/x"], "--tools")
        assert "'amiga' is not a rez platform" in output.getvalue()

    def test_none_is_empty(self) -> None:
        assert _platform_values(None, "--tools") == {}


class TestIsAbsoluteFor:
    @pytest.mark.parametrize(
        ("path", "platform", "expected"),
        [
            ("P:/rez/bin", "windows", True),
            ("\\\\server\\share\\rez", "windows", True),
            ("//server/share/rez", "windows", True),
            ("rez/bin", "windows", False),
            ("/mnt/p/rez/bin", "linux", True),
            ("rez/bin", "osx", False),
        ],
    )
    def test_shape_per_platform(self, path: str, platform: str, *, expected: bool) -> None:
        assert _is_absolute_for(path, platform) is expected


class TestNextLaunchVersion:
    def test_first_version(self, tmp_path: Path) -> None:
        assert _next_launch_version(BuildTarget(store=tmp_path, source="x")) == "1.0.0"
        assert _next_launch_version(None) == "1.0.0"

    def test_one_patch_above_newest(self, tmp_path: Path) -> None:
        for version in ("1.0.0", "1.0.9", "1.0.10"):
            package = tmp_path / "griptape_launch" / version / "package.py"
            package.parent.mkdir(parents=True)
            package.write_text("")
        assert _next_launch_version(BuildTarget(store=tmp_path, source="x")) == "1.0.11"

    def test_non_numeric_patch_gets_a_suffix(self, tmp_path: Path) -> None:
        package = tmp_path / "griptape_launch" / "2.0.beta" / "package.py"
        package.parent.mkdir(parents=True)
        package.write_text("")
        assert _next_launch_version(BuildTarget(store=tmp_path, source="x")) == "2.0.beta.1"


class TestWriteLaunchPackageCommand:
    @pytest.fixture
    def engine_store(self, tmp_path: Path, rez_on: MagicMock) -> Path:
        store = tmp_path / "store"
        engine = store / "griptape_nodes_engine" / "0.103.0" / "package.py"
        engine.parent.mkdir(parents=True)
        engine.write_text("")
        rez_on.return_value = _setup(local=store)
        return store

    @pytest.mark.usefixtures("output")
    def test_print_shows_the_package(self, engine_store: Path) -> None:
        with patch(f"{MODULE}.rez_package_stores", return_value=[]):
            result = runner.invoke(app, ["write-launch-package", "--print", "--tools", "windows=P:/rez/bin"])
        assert result.exit_code == 0, result.output
        assert "requires = ['griptape_nodes_engine-0.103.0']" in result.output
        assert "'windows': {'GTN_REZ_BIN_PATH': 'P:/rez/bin'}" in result.output
        assert not (engine_store / "griptape_launch").exists()

    @pytest.mark.usefixtures("output")
    def test_writes_testing_package_and_validates(self, engine_store: Path) -> None:
        with (
            patch(f"{MODULE}.rez_package_stores", return_value=[]),
            patch(f"{MODULE}._validate_launch_package") as validate,
        ):
            result = runner.invoke(
                app,
                [
                    "write-launch-package",
                    "--tools",
                    "osx=/Volumes/p/rez/bin",
                    "--testing-store",
                    "osx=/Volumes/p/rez/local",
                    "--config-file",
                    "osx=/Volumes/p/rez/griptape.py",
                    "--set",
                    "XDG_CONFIG_HOME=/Volumes/p/config",
                ],
            )
        assert result.exit_code == 0, result.output
        validate.assert_called_once_with("1.0.0")
        content = (engine_store / "griptape_launch" / "1.0.0" / "package.py").read_text()
        assert _Rex("osx").run(content) == {
            "GTN_REZ_BIN_PATH": "/Volumes/p/rez/bin",
            "GTN_REZ_LOCAL_PACKAGES_PATH": "/Volumes/p/rez/local",
            "GTN_REZ_CONFIG_FILE": "/Volumes/p/rez/griptape.py",
            "XDG_CONFIG_HOME": "/Volumes/p/config",
        }

    @pytest.mark.usefixtures("output")
    def test_defaults_to_this_machines_tools(self, engine_store: Path) -> None:
        with (
            patch(f"{MODULE}.rez_package_stores", return_value=[]),
            patch(f"{MODULE}._validate_launch_package"),
            patch(f"{MODULE}.current_platform_key", return_value="linux"),
        ):
            result = runner.invoke(app, ["write-launch-package"])
        assert result.exit_code == 0, result.output
        content = (engine_store / "griptape_launch" / "1.0.0" / "package.py").read_text()
        assert _Rex("linux").run(content)["GTN_REZ_BIN_PATH"] == str(TOOLS)

    def test_existing_version_is_not_replaced(self, engine_store: Path, output: io.StringIO) -> None:
        existing = engine_store / "griptape_launch" / "3.0.0" / "package.py"
        existing.parent.mkdir(parents=True)
        existing.write_text("original")
        with patch(f"{MODULE}.rez_package_stores", return_value=[]):
            result = runner.invoke(app, ["write-launch-package", "--version", "3.0.0", "--tools", "/x/bin"])
        assert result.exit_code == 1
        assert existing.read_text() == "original"
        assert "--rebuild" in output.getvalue()

    @pytest.mark.usefixtures("output")
    def test_rebuild_replaces_existing_version(self, engine_store: Path) -> None:
        existing = engine_store / "griptape_launch" / "3.0.0" / "package.py"
        existing.parent.mkdir(parents=True)
        existing.write_text("original")
        with (
            patch(f"{MODULE}.rez_package_stores", return_value=[]),
            patch(f"{MODULE}._validate_launch_package"),
        ):
            result = runner.invoke(
                app, ["write-launch-package", "--version", "3.0.0", "--tools", "/x/bin", "--rebuild"]
            )
        assert result.exit_code == 0, result.output
        assert "griptape_launch" in existing.read_text()

    def test_relative_tools_need_a_root(self, engine_store: Path, output: io.StringIO) -> None:
        with patch(f"{MODULE}.rez_package_stores", return_value=[]):
            result = runner.invoke(app, ["write-launch-package", "--tools", "linux=rez/bin"])
        assert result.exit_code == 1
        assert "--root linux=<path>" in output.getvalue()
        assert not (engine_store / "griptape_launch").exists()

    @pytest.mark.usefixtures("output")
    def test_relative_tools_with_root_become_a_path_map(self, engine_store: Path) -> None:
        with (
            patch(f"{MODULE}.rez_package_stores", return_value=[]),
            patch(f"{MODULE}._validate_launch_package"),
        ):
            result = runner.invoke(
                app, ["write-launch-package", "--tools", "linux=rez/bin", "--root", "linux=/mnt/pipeline"]
            )
        assert result.exit_code == 0, result.output
        content = (engine_store / "griptape_launch" / "1.0.0" / "package.py").read_text()
        assert _Rex("linux").run(content) == {"GTN_REZ_BIN_PATH": "rez/bin", "GTN_REZ_PATH_MAP": "linux=/mnt/pipeline"}

    def test_bad_set_value_stops(self, engine_store: Path, output: io.StringIO) -> None:
        with patch(f"{MODULE}.rez_package_stores", return_value=[]):
            result = runner.invoke(app, ["write-launch-package", "--print", "--tools", "/x", "--set", "NOVALUE"])
        assert result.exit_code == 1
        assert "not NAME=VALUE" in output.getvalue()
        assert not (engine_store / "griptape_launch").exists()

    def test_no_engine_package_stops(self, tmp_path: Path, output: io.StringIO, rez_on: MagicMock) -> None:
        rez_on.return_value = _setup(local=tmp_path / "empty")
        with patch(f"{MODULE}.rez_package_stores", return_value=[]):
            result = runner.invoke(app, ["write-launch-package", "--print", "--tools", "/x"])
        assert result.exit_code == 1
        assert "build-engine-package" in output.getvalue()

    @pytest.mark.usefixtures("output")
    def test_explicit_engine_version_needs_no_engine_package(self, tmp_path: Path, rez_on: MagicMock) -> None:
        rez_on.return_value = _setup(local=tmp_path / "empty")
        result = runner.invoke(app, ["write-launch-package", "--print", "--tools", "/x", "--engine-version", "9.9.9"])
        assert result.exit_code == 0, result.output
        assert "griptape_nodes_engine-9.9.9" in result.output

    @pytest.mark.usefixtures("output")
    def test_engine_found_on_rez_search_path(self, tmp_path: Path, rez_on: MagicMock) -> None:
        released = tmp_path / "release"
        engine = released / "griptape_nodes_engine" / "0.99.0" / "package.py"
        engine.parent.mkdir(parents=True)
        engine.write_text("")
        rez_on.return_value = _setup()
        with patch(f"{MODULE}.rez_package_stores", return_value=[released]):
            result = runner.invoke(app, ["write-launch-package", "--print", "--tools", "/x"])
        assert result.exit_code == 0, result.output
        assert "griptape_nodes_engine-0.99.0" in result.output

    def test_no_tools_anywhere_stops(self, output: io.StringIO) -> None:
        with patch(f"{MODULE}.rez_setup", return_value=_setup(enabled=False)):
            result = runner.invoke(app, ["write-launch-package", "--print"])
        assert result.exit_code == 1
        assert "--tools platform=path" in output.getvalue()

    @pytest.mark.usefixtures("output")
    def test_interactive_asks_for_each_platform(self, engine_store: Path) -> None:
        answers = {"linux": "", "osx": "/Volumes/p/rez/bin", "windows": "P:/rez/bin"}

        def fake_prompt(question: str, **_: object) -> str:
            platform = question.split(" on ")[1].split("?", maxsplit=1)[0]
            return answers[platform]

        with (
            patch(f"{MODULE}._can_prompt", return_value=True),
            patch(f"{MODULE}.typer.prompt", side_effect=fake_prompt),
            patch(f"{MODULE}.rez_package_stores", return_value=[]),
            patch(f"{MODULE}._validate_launch_package"),
        ):
            result = runner.invoke(app, ["write-launch-package"])
        assert result.exit_code == 0, result.output
        content = (engine_store / "griptape_launch" / "1.0.0" / "package.py").read_text()
        assert "'linux'" not in content
        assert _Rex("windows").run(content)["GTN_REZ_BIN_PATH"] == "P:/rez/bin"


class TestValidateLaunchPackage:
    def test_resolves_and_says_how_to_start(self, output: io.StringIO) -> None:
        with (
            patch(f"{MODULE}._rez_executable", side_effect=lambda name: name),
            patch(f"{MODULE}.subprocess.run", return_value=_completed(0)) as run,
        ):
            rez._validate_launch_package("1.0.0")
        assert run.call_args.args[0][:3] == ["rez", "env", "griptape_launch-1.0.0"]
        text = output.getvalue()
        assert "resolves and imports griptape_nodes" in text
        assert "rez-env griptape_launch -- gtn" in text

    def test_resolve_failure_shows_stderr(self, output: io.StringIO) -> None:
        with (
            patch(f"{MODULE}._rez_executable", side_effect=lambda name: name),
            patch(f"{MODULE}.subprocess.run", return_value=_completed(1, stderr="PackageNotFound: x")),
        ):
            rez._validate_launch_package("1.0.0")
        assert "PackageNotFound: x" in output.getvalue()

    def test_subprocess_error_is_reported(self, output: io.StringIO) -> None:
        with (
            patch(f"{MODULE}._rez_executable", side_effect=lambda name: name),
            patch(f"{MODULE}.subprocess.run", side_effect=OSError("boom")),
        ):
            rez._validate_launch_package("1.0.0")
        assert "Could not check griptape_launch-1.0.0: boom" in output.getvalue()


class TestApp:
    def test_registers_all_commands(self) -> None:
        names = {command.name for command in rez.app.registered_commands}
        assert names == {"build-engine-package", "build-library-package", "write-launch-package"}


# ---------------------------------------------------------------------------
# Shared stores and torch builds
# ---------------------------------------------------------------------------


class TestInstallReportOutput:
    def test_reports_other_platforms_and_what_was_added(self, output: io.StringIO) -> None:
        report = InstallReport(
            new=["a==1"],
            added_variant=["torch==2.7.0", "numpy==2.1.0"],
            already_installed=["six==1.16.0"],
            other_platforms={"osx/arm64"},
        )
        with patch(f"{MODULE}.rez_platform_key", return_value="windows-AMD64"):
            rez._print_install_report(report)
        text = output.getvalue()
        assert "already has packages built for osx/arm64" in text
        assert "1 new; 2 gained a windows/AMD64 variant; 1 already installed for windows/AMD64" in text

    def test_first_build_says_nothing_about_other_platforms(self, output: io.StringIO) -> None:
        rez._print_install_report(InstallReport(new=["a==1"], rebuilt=["b==2"]))
        text = output.getvalue()
        assert "already has packages" not in text
        assert "1 rebuilt" in text

    def test_engine_dependency_install_prints_the_report(self, tmp_path: Path, output: io.StringIO) -> None:
        with patch(f"{MODULE}.rez_uv_install", return_value=InstallReport(new=["requests==2.32.3"])):
            _install_deps(["requests"], tmp_path, skip_installed=True)
        assert "Packages: 1 new" in output.getvalue()


class TestTorchBackendOption:
    def test_none_when_not_given(self) -> None:
        assert rez._torch_backends(None) is None

    def test_named_builds_are_kept_in_order_without_duplicates(self) -> None:
        with patch(f"{MODULE}.current_platform_key", return_value="windows"):
            assert rez._torch_backends(["cu128", "cu118", "cu128"]) == ["cu128", "cu118"]

    def test_all(self) -> None:
        with patch(f"{MODULE}.current_platform_key", return_value="linux"):
            assert rez._torch_backends(["cu118", "all"]) == ["all"]

    def test_unknown_build_stops(self, output: io.StringIO) -> None:
        with pytest.raises(typer.Exit):
            rez._torch_backends(["cu999"])
        assert "not a torch build uv knows" in output.getvalue()

    def test_ignored_on_macos(self, output: io.StringIO) -> None:
        with patch(f"{MODULE}.current_platform_key", return_value="osx"):
            assert rez._torch_backends(["cu128"]) is None
        assert "ignored on macOS" in output.getvalue()

    @pytest.mark.usefixtures("output", "rez_on")
    def test_command_passes_builds_to_the_library_build(self, tmp_path: Path) -> None:
        with (
            patch(f"{MODULE}.current_platform_key", return_value="windows"),
            patch(f"{MODULE}._check_rez_bindings"),
            patch(f"{MODULE}._build_library_from_local") as build,
        ):
            result = runner.invoke(
                app,
                [
                    "build-library-package",
                    "--local-path",
                    str(tmp_path),
                    "--local-packages-path",
                    str(tmp_path / "store"),
                    "--torch-backend",
                    "cu118",
                    "--torch-backend",
                    "cu128",
                ],
            )
        assert result.exit_code == 0, result.output
        options = build.call_args.args[1]
        assert options.torch_backends == ["cu118", "cu128"]


class TestLibraryBuildTorchOutput:
    @pytest.fixture(autouse=True)
    def _no_git(self) -> Iterator[None]:
        with patch("griptape_nodes.utils.rez_utils.get_git_repository_root", return_value=None):
            yield

    @pytest.mark.usefixtures("clean_env")
    def test_reports_built_and_skipped_torch_builds(self, tmp_path: Path, output: io.StringIO) -> None:
        library_dir = _make_library(tmp_path, {"name": "Torchy"})
        result = LibraryInstallResult(
            report=InstallReport(added_variant=["torch==2.7.0"]),
            built_torch_backends=["cu118", "cu128"],
            skipped_torch_backends=["cu130"],
        )
        options = LibraryBuildOptions(
            store=tmp_path / "store", skip_installed=True, interactive=False, torch_backends=["all"]
        )
        with (
            patch(f"{MODULE}.install_library_as_rez_package", return_value=result) as install,
            patch(f"{MODULE}.get_library_rez_package_version", return_value="1.0.0"),
            patch(f"{MODULE}._validate_rez_package"),
        ):
            _build_library_from_dir(library_dir, options)
        assert install.call_args.kwargs["torch_backends"] == ["all"]
        text = output.getvalue()
        assert "Torch builds:  all" in text
        assert "Torch builds installed: cu118, cu128" in text
        assert "Torch builds skipped (they do not publish the pinned torch version): cu130" in text
        assert "1 gained a" in text

    @pytest.mark.usefixtures("clean_env")
    def test_no_torch_build_available_stops_with_reason(self, tmp_path: Path, output: io.StringIO) -> None:
        library_dir = _make_library(tmp_path, {"name": "Torchy"})
        error = RuntimeError("none of them publish the torch version the library pins")
        with (
            patch(f"{MODULE}.install_library_as_rez_package", side_effect=error),
            pytest.raises(typer.Exit),
        ):
            _build_library_from_dir(library_dir, _options(tmp_path / "store"))
        assert "none of them publish the torch version" in output.getvalue()
