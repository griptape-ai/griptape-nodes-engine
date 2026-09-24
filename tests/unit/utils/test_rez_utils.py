"""Unit tests for rez_utils module."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from griptape_nodes.utils.rez_utils import (
    _anchor_drive_letter,
    _derive_library_version,
    _detect_rez_store_family,
    _executable_candidate_names,
    _git_describe_version,
    _git_remote_repo_name,
    _library_rez_name,
    _parse_rez_spec,
    _read_pyproject_version,
    _resolve_rez_path,
    _rez_executable,
    _write_library_meta_package,
    build_direct_requires,
    build_rez_env_prefix,
    check_rez_health,
    check_rez_health_detailed,
    current_platform_key,
    find_library_manifest,
    get_library_rez_package_version,
    get_rez_context_string,
    install_library_as_rez_package,
    is_in_rez_context,
    is_library_rez_package_available,
    is_rez_enabled,
    is_rez_library_path,
    library_edit_rez_requests,
    library_file_path_to_rez_family,
    pip_spec_name,
    read_library_dependencies,
    read_library_manifest,
    read_library_package_requires,
    resolve_and_log_rez_context,
    resolve_rez_library_json_path,
    resolve_rez_pythonpath,
    rez_bin_path,
    rez_config_file,
    rez_library_package_name,
    rez_library_package_version,
    rez_local_packages_path,
    rez_path_map,
    rez_release_packages_path,
    rez_root,
    rez_search_paths,
    rez_subprocess_env,
    rez_unsearched_stores,
    rez_version_from_git_ref,
)
from griptape_nodes.utils.rez_uv import ResolvedPackage, RezInstallError

if TYPE_CHECKING:
    from collections.abc import Iterator

# ---------------------------------------------------------------------------
# Rez enabled detection
# ---------------------------------------------------------------------------


class TestRezEnabled:
    def test_disabled_when_root_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("GTN_REZ_ROOT", None)
            assert not is_rez_enabled()

    def test_enabled_when_root_set(self) -> None:
        with patch.dict(os.environ, {"GTN_REZ_ROOT": "/mnt/pipeline"}):
            assert is_rez_enabled()

    def test_disabled_when_root_empty(self) -> None:
        with patch.dict(os.environ, {"GTN_REZ_ROOT": ""}):
            assert not is_rez_enabled()

    def test_disabled_when_root_whitespace(self) -> None:
        with patch.dict(os.environ, {"GTN_REZ_ROOT": "   "}):
            assert not is_rez_enabled()


# ---------------------------------------------------------------------------
# Name normalisation
# ---------------------------------------------------------------------------


class TestLibraryRezName:
    def test_hyphens_to_underscores(self) -> None:
        assert _library_rez_name("griptape-nodes-library-diffusers") == "griptape_nodes_library_diffusers"

    def test_dots_to_underscores(self) -> None:
        assert _library_rez_name("my.library.name") == "my_library_name"

    def test_spaces_to_underscores(self) -> None:
        assert _library_rez_name("My Library Name") == "my_library_name"

    def test_lowercased(self) -> None:
        assert _library_rez_name("MyLibrary") == "mylibrary"

    def test_mixed_separators(self) -> None:
        assert _library_rez_name("my-lib.name_v2") == "my_lib_name_v2"


class TestPipSpecName:
    def test_bare_name(self) -> None:
        assert pip_spec_name("torch") == "torch"

    def test_version_constraint(self) -> None:
        assert pip_spec_name("torch>=2.0,<3") == "torch"

    def test_extras(self) -> None:
        assert pip_spec_name("diffusers[torch]==0.39.0") == "diffusers"

    def test_tilde_constraint(self) -> None:
        assert pip_spec_name("numpy~=1.26") == "numpy"

    def test_no_leading_whitespace(self) -> None:
        assert pip_spec_name("pillow >= 11.0") == "pillow"


# ---------------------------------------------------------------------------
# REZ: path parsing
# ---------------------------------------------------------------------------


class TestRezLibraryPath:
    def test_is_rez_path(self) -> None:
        assert is_rez_library_path("REZ:griptape_nodes_library_standard")

    def test_not_rez_path(self) -> None:
        assert not is_rez_library_path("/path/to/library.json")

    def test_rez_package_name(self) -> None:
        assert rez_library_package_name("REZ:griptape_nodes_library_standard") == "griptape_nodes_library_standard"

    def test_rez_package_name_with_version(self) -> None:
        assert (
            rez_library_package_name("REZ:griptape_nodes_library_standard-0.81.0") == "griptape_nodes_library_standard"
        )

    def test_rez_package_version_present(self) -> None:
        assert rez_library_package_version("REZ:lib-1.0.0") == "1.0.0"

    def test_rez_package_version_absent(self) -> None:
        assert rez_library_package_version("REZ:lib") is None


class TestParseRezSpec:
    def test_family_only(self) -> None:
        assert _parse_rez_spec("griptape_nodes_library_standard") == ("griptape_nodes_library_standard", None)

    def test_family_with_version(self) -> None:
        assert _parse_rez_spec("griptape_nodes_library_standard-0.81.0") == (
            "griptape_nodes_library_standard",
            "0.81.0",
        )

    def test_no_version_when_not_digit(self) -> None:
        assert _parse_rez_spec("my-library-name") == ("my-library-name", None)

    def test_version_starts_with_digit(self) -> None:
        assert _parse_rez_spec("lib-2.0") == ("lib", "2.0")


# ---------------------------------------------------------------------------
# Rez store family detection
# ---------------------------------------------------------------------------


class TestDetectRezStoreFamily:
    def test_detects_store_layout(self, tmp_path: Path) -> None:
        family = "griptape_nodes_library_test"
        version_dir = tmp_path / "local" / family / "1.0.0"
        python_dir = version_dir / "python"
        python_dir.mkdir(parents=True)
        (version_dir / "package.py").write_text("name = 'test'")
        manifest = python_dir / "griptape_nodes_library.json"
        manifest.write_text("{}")

        assert _detect_rez_store_family(manifest) == family

    def test_returns_none_for_non_store(self, tmp_path: Path) -> None:
        manifest = tmp_path / "mylib" / "griptape_nodes_library.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("{}")

        assert _detect_rez_store_family(manifest) is None

    def test_returns_none_without_package_py(self, tmp_path: Path) -> None:
        version_dir = tmp_path / "local" / "test" / "1.0.0"
        python_dir = version_dir / "python"
        python_dir.mkdir(parents=True)
        manifest = python_dir / "lib.json"
        manifest.write_text("{}")

        assert _detect_rez_store_family(manifest) is None


# ---------------------------------------------------------------------------
# Manifest reading
# ---------------------------------------------------------------------------


class TestFindLibraryManifest:
    def test_finds_underscore_variant(self, tmp_path: Path) -> None:
        manifest = tmp_path / "griptape_nodes_library.json"
        manifest.write_text("{}")
        assert find_library_manifest(tmp_path) == manifest

    def test_finds_hyphen_variant(self, tmp_path: Path) -> None:
        manifest = tmp_path / "griptape-nodes-library.json"
        manifest.write_text("{}")
        assert find_library_manifest(tmp_path) == manifest

    def test_finds_in_subdirectory(self, tmp_path: Path) -> None:
        subdir = tmp_path / "src"
        subdir.mkdir()
        manifest = subdir / "griptape_nodes_library.json"
        manifest.write_text("{}")
        assert find_library_manifest(tmp_path) == manifest

    def test_returns_none_when_missing(self, tmp_path: Path) -> None:
        assert find_library_manifest(tmp_path) is None


class TestReadLibraryManifest:
    def test_reads_name_and_deps(self, tmp_path: Path) -> None:
        manifest = tmp_path / "lib.json"
        manifest.write_text(
            json.dumps(
                {
                    "name": "Test Library",
                    "metadata": {
                        "dependencies": {
                            "pip_dependencies": ["numpy>=1.26", "torch>=2.0"],
                        }
                    },
                }
            )
        )
        name, deps, _exec_deps, _flags = read_library_manifest(manifest)
        assert name == "Test Library"
        assert deps == ["numpy>=1.26", "torch>=2.0"]

    def test_splits_exec_deps(self, tmp_path: Path) -> None:
        manifest = tmp_path / "lib.json"
        manifest.write_text(
            json.dumps(
                {
                    "name": "Test Library",
                    "metadata": {
                        "dependencies": {
                            "pip_dependencies": ["numpy"],
                            "pip_dependencies_exec": ["torch"],
                        }
                    },
                }
            )
        )
        _name, deps, exec_deps, _flags = read_library_manifest(manifest)
        assert deps == ["numpy"]
        assert exec_deps == ["torch"]

    def test_empty_deps(self, tmp_path: Path) -> None:
        manifest = tmp_path / "lib.json"
        manifest.write_text(json.dumps({"name": "Empty", "metadata": {}}))
        name, deps, exec_deps, flags = read_library_manifest(manifest)
        assert name == "Empty"
        assert deps == []
        assert exec_deps == []
        assert flags == []

    def test_invalid_json(self, tmp_path: Path) -> None:
        manifest = tmp_path / "lib.json"
        manifest.write_text("not json")
        name, deps, exec_deps, flags = read_library_manifest(manifest)
        assert name == ""
        assert deps == []
        assert exec_deps == []
        assert flags == []


class TestReadLibraryDependencies:
    def test_reads_library_deps(self, tmp_path: Path) -> None:
        manifest = tmp_path / "lib.json"
        manifest.write_text(
            json.dumps(
                {
                    "name": "Test",
                    "metadata": {
                        "declarations": [
                            {"type": "library_dependency", "url": "https://github.com/org/lib-a.git", "required": True},
                            {
                                "type": "library_dependency",
                                "url": "https://github.com/org/lib-b.git",
                                "required": False,
                            },
                            {"type": "lifecycle_stage", "stage": "stable"},
                        ]
                    },
                }
            )
        )
        deps = read_library_dependencies(manifest)
        assert len(deps) == 2  # noqa: PLR2004
        assert deps[0]["url"] == "https://github.com/org/lib-a.git"
        assert deps[0]["required"] is True
        assert deps[1]["required"] is False

    def test_no_declarations(self, tmp_path: Path) -> None:
        manifest = tmp_path / "lib.json"
        manifest.write_text(json.dumps({"name": "Test", "metadata": {}}))
        assert read_library_dependencies(manifest) == []


# ---------------------------------------------------------------------------
# Library file path to rez family
# ---------------------------------------------------------------------------


class TestLibraryFilePathToRezFamily:
    def test_rez_store_path(self, tmp_path: Path) -> None:
        family = "griptape_nodes_library_test"
        version_dir = tmp_path / "local" / family / "1.0.0"
        python_dir = version_dir / "python"
        python_dir.mkdir(parents=True)
        (version_dir / "package.py").write_text("name = 'test'")
        manifest = python_dir / "griptape_nodes_library.json"
        manifest.write_text("{}")

        result = library_file_path_to_rez_family(manifest)
        assert result == family

    def test_non_git_parent_dir(self, tmp_path: Path) -> None:
        lib_dir = tmp_path / "my_custom_library"
        lib_dir.mkdir()
        manifest = lib_dir / "griptape_nodes_library.json"
        manifest.write_text("{}")

        result = library_file_path_to_rez_family(manifest)
        assert result == "my_custom_library"

    def test_normalises_name(self, tmp_path: Path) -> None:
        lib_dir = tmp_path / "My-Library.Name"
        lib_dir.mkdir()
        manifest = lib_dir / "griptape_nodes_library.json"
        manifest.write_text("{}")

        result = library_file_path_to_rez_family(manifest)
        assert result == "my_library_name"


# ---------------------------------------------------------------------------
# GTN_REZ_ROOT and GTN_REZ_PATH_MAP
# ---------------------------------------------------------------------------


class TestRezRoot:
    def test_returns_path_when_set(self) -> None:
        with patch.dict(os.environ, {"GTN_REZ_ROOT": "/mnt/pipeline"}):
            result = rez_root()
            assert result == Path("/mnt/pipeline")

    def test_returns_none_when_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("GTN_REZ_ROOT", None)
            assert rez_root() is None


class TestRezPathMap:
    def test_parses_all_platforms(self) -> None:
        with patch.dict(os.environ, {"GTN_REZ_PATH_MAP": "linux=/mnt/pipeline;osx=/Volumes/pipeline;windows=P:"}):
            result = rez_path_map()
            assert result == {"linux": "/mnt/pipeline", "osx": "/Volumes/pipeline", "windows": "P:"}

    def test_single_platform(self) -> None:
        with patch.dict(os.environ, {"GTN_REZ_PATH_MAP": "linux=/mnt/pipeline"}):
            result = rez_path_map()
            assert result == {"linux": "/mnt/pipeline"}

    def test_empty_string(self) -> None:
        with patch.dict(os.environ, {"GTN_REZ_PATH_MAP": ""}):
            assert rez_path_map() == {}

    def test_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("GTN_REZ_PATH_MAP", None)
            assert rez_path_map() == {}

    def test_handles_whitespace(self) -> None:
        with patch.dict(os.environ, {"GTN_REZ_PATH_MAP": " linux = /mnt/pipeline ; windows = P: "}):
            result = rez_path_map()
            assert result == {"linux": "/mnt/pipeline", "windows": "P:"}


class TestCurrentPlatformKey:
    def test_linux(self) -> None:
        import sys

        with patch.object(sys, "platform", "linux"):
            assert current_platform_key() == "linux"

    def test_darwin(self) -> None:
        import sys

        with patch.object(sys, "platform", "darwin"):
            assert current_platform_key() == "osx"

    def test_windows(self) -> None:
        import sys

        with patch.object(sys, "platform", "win32"):
            assert current_platform_key() == "windows"


class TestResolveRezPath:
    def test_relative_with_root(self) -> None:
        with patch.dict(os.environ, {"GTN_REZ_ROOT": "/mnt/pipeline", "GTN_REZ_BIN_PATH": "rez/bin"}):
            result = _resolve_rez_path("GTN_REZ_BIN_PATH")
            assert result == Path("/mnt/pipeline/rez/bin")

    def test_relative_without_root(self) -> None:
        with patch.dict(os.environ, {"GTN_REZ_BIN_PATH": "rez/bin"}, clear=True):
            os.environ.pop("GTN_REZ_ROOT", None)
            result = _resolve_rez_path("GTN_REZ_BIN_PATH")
            assert result == Path("rez/bin")

    def test_absolute_path_skips_root(self) -> None:
        with patch.dict(
            os.environ, {"GTN_REZ_ROOT": "/mnt/pipeline", "GTN_REZ_LOCAL_PACKAGES_PATH": "/home/user/rez/local"}
        ):
            result = _resolve_rez_path("GTN_REZ_LOCAL_PACKAGES_PATH")
            assert result == Path("/home/user/rez/local")

    def test_unset_returns_none(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("GTN_REZ_BIN_PATH", None)
            assert _resolve_rez_path("GTN_REZ_BIN_PATH") is None

    def test_windows_drive_is_absolute(self) -> None:
        with patch.dict(os.environ, {"GTN_REZ_ROOT": "P:", "GTN_REZ_LOCAL_PACKAGES_PATH": "rez/packages/local"}):
            result = _resolve_rez_path("GTN_REZ_LOCAL_PACKAGES_PATH")
            assert result == Path("P:/rez/packages/local")


class TestAnchorDriveLetter:
    def test_bare_drive_gets_root(self) -> None:
        assert _anchor_drive_letter("P:") == "P:/"

    def test_drive_relative_path_gets_root(self) -> None:
        assert _anchor_drive_letter("P:pipeline") == "P:/pipeline"

    def test_rooted_drive_unchanged(self) -> None:
        assert _anchor_drive_letter("P:/pipeline") == "P:/pipeline"
        assert _anchor_drive_letter("P:\\pipeline") == "P:\\pipeline"

    def test_posix_and_unc_unchanged(self) -> None:
        assert _anchor_drive_letter("/mnt/pipeline") == "/mnt/pipeline"
        assert _anchor_drive_letter("\\\\server\\share") == "\\\\server\\share"


class TestRezExecutable:
    def test_bare_command_when_no_bin_path(self) -> None:
        from griptape_nodes.utils.rez_utils import _rez_executable

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("GTN_REZ_BIN_PATH", None)
            os.environ.pop("GTN_REZ_ROOT", None)
            result = _rez_executable("rez-nonexistent-test-cmd")
            assert result == "rez-nonexistent-test-cmd"

    def test_resolves_via_which_with_bin_path(self, tmp_path: Path) -> None:
        from griptape_nodes.utils.rez_utils import _rez_executable

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_rez = bin_dir / "rez"
        fake_rez.write_text("#!/bin/sh\necho ok")
        fake_rez.chmod(0o755)

        with patch.dict(os.environ, {"GTN_REZ_BIN_PATH": str(bin_dir)}):
            os.environ.pop("GTN_REZ_ROOT", None)
            result = _rez_executable("rez")
            assert "rez" in result


# ---------------------------------------------------------------------------
# Helpers for subprocess-backed tests
# ---------------------------------------------------------------------------


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


_RU = "griptape_nodes.utils.rez_utils"


# ---------------------------------------------------------------------------
# Path accessors
# ---------------------------------------------------------------------------


class TestPathAccessors:
    def test_accessors_resolve_relative_to_root(self, tmp_path: Path) -> None:
        env = {
            "GTN_REZ_ROOT": str(tmp_path),
            "GTN_REZ_BIN_PATH": "rez/bin",
            "GTN_REZ_CONFIG_FILE": "rez/rezconfig.py",
            "GTN_REZ_LOCAL_PACKAGES_PATH": "rez/packages/local",
            "GTN_REZ_RELEASE_PACKAGES_PATH": "rez/packages/release",
        }
        with patch.dict(os.environ, env, clear=True):
            assert rez_bin_path() == tmp_path / "rez/bin"
            assert rez_config_file() == tmp_path / "rez/rezconfig.py"
            assert rez_local_packages_path() == tmp_path / "rez/packages/local"
            assert rez_release_packages_path() == tmp_path / "rez/packages/release"

    def test_relative_path_without_root_returned_as_is(self) -> None:
        with patch.dict(os.environ, {"GTN_REZ_BIN_PATH": "rez/bin"}, clear=True):
            assert rez_bin_path() == Path("rez/bin")

    def test_absolute_path_ignores_root(self, tmp_path: Path) -> None:
        absolute = tmp_path / "elsewhere" / "local"
        with patch.dict(
            os.environ, {"GTN_REZ_ROOT": "/ignored", "GTN_REZ_LOCAL_PACKAGES_PATH": str(absolute)}, clear=True
        ):
            assert rez_local_packages_path() == absolute


# ---------------------------------------------------------------------------
# Executable lookup
# ---------------------------------------------------------------------------


class TestExecutableCandidateNames:
    def test_posix_returns_command_only(self) -> None:
        with patch(f"{_RU}.os.name", "posix"):
            assert _executable_candidate_names("rez") == ["rez"]

    def test_windows_enumerates_pathext(self) -> None:
        with patch(f"{_RU}.os.name", "nt"), patch.dict(os.environ, {"PATHEXT": os.pathsep.join([".EXE", ".CMD"])}):
            names = _executable_candidate_names("rez")
        assert names[-1] == "rez"
        assert "rez.EXE" in names
        assert "rez.CMD" in names

    def test_windows_keeps_command_with_extension(self) -> None:
        with patch(f"{_RU}.os.name", "nt"), patch.dict(os.environ, {"PATHEXT": os.pathsep.join([".EXE", ".CMD"])}):
            assert _executable_candidate_names("rez.exe") == ["rez.exe"]


class TestRezExecutableLookup:
    def test_prefers_bin_path_candidate(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        exe = bin_dir / _executable_candidate_names("rez-search")[0]
        exe.write_text("")
        exe.chmod(0o755)
        with patch.dict(os.environ, {"GTN_REZ_BIN_PATH": str(bin_dir)}, clear=True):
            assert _rez_executable("rez-search") == str(exe)

    def test_falls_back_to_which(self, tmp_path: Path) -> None:
        with (
            patch.dict(os.environ, {"GTN_REZ_BIN_PATH": str(tmp_path / "missing")}, clear=True),
            patch(f"{_RU}.shutil.which", return_value="/usr/bin/rez"),
        ):
            assert _rez_executable("rez") == "/usr/bin/rez"


# ---------------------------------------------------------------------------
# Rez store lookups
# ---------------------------------------------------------------------------


def _make_rez_version(store_local: Path, family: str, version: str, *, manifest: str | None = None) -> Path:
    version_dir = store_local / family / version
    version_dir.mkdir(parents=True)
    (version_dir / "package.py").write_text(f"name = '{family}'\nversion = '{version}'\n")
    if manifest is not None:
        python_dir = version_dir / "python"
        python_dir.mkdir()
        (python_dir / manifest).write_text("{}")
    return version_dir


class TestResolveRezLibraryJsonPath:
    def test_no_store_configured(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert resolve_rez_library_json_path("anything") is None

    def test_missing_family(self, tmp_path: Path) -> None:
        local = tmp_path / "local"
        local.mkdir()
        with patch.dict(os.environ, {"GTN_REZ_LOCAL_PACKAGES_PATH": str(local)}, clear=True):
            assert resolve_rez_library_json_path("nope") is None

    def test_latest_version_is_chosen_numerically(self, tmp_path: Path) -> None:
        local = tmp_path / "local"
        _make_rez_version(local, "my_lib", "1.9.0", manifest="griptape_nodes_library.json")
        newest = _make_rez_version(local, "my_lib", "1.10.0", manifest="griptape_nodes_library.json")
        with patch.dict(os.environ, {"GTN_REZ_LOCAL_PACKAGES_PATH": str(local)}, clear=True):
            result = resolve_rez_library_json_path("my_lib")
        assert result == newest / "python" / "griptape_nodes_library.json"

    def test_pinned_version(self, tmp_path: Path) -> None:
        local = tmp_path / "local"
        pinned = _make_rez_version(local, "my_lib", "1.0.0", manifest="griptape-nodes-library.json")
        _make_rez_version(local, "my_lib", "2.0.0", manifest="griptape_nodes_library.json")
        with patch.dict(os.environ, {"GTN_REZ_LOCAL_PACKAGES_PATH": str(local)}, clear=True):
            result = resolve_rez_library_json_path("my_lib", version="1.0.0")
        assert result == pinned / "python" / "griptape-nodes-library.json"

    def test_pinned_version_missing(self, tmp_path: Path) -> None:
        local = tmp_path / "local"
        _make_rez_version(local, "my_lib", "1.0.0", manifest="griptape_nodes_library.json")
        with patch.dict(os.environ, {"GTN_REZ_LOCAL_PACKAGES_PATH": str(local)}, clear=True):
            assert resolve_rez_library_json_path("my_lib", version="9.9.9") is None

    def test_family_without_versions(self, tmp_path: Path) -> None:
        local = tmp_path / "local"
        (local / "my_lib" / "junk").mkdir(parents=True)
        with patch.dict(os.environ, {"GTN_REZ_LOCAL_PACKAGES_PATH": str(local)}, clear=True):
            assert resolve_rez_library_json_path("my_lib") is None

    def test_version_without_python_dir(self, tmp_path: Path) -> None:
        local = tmp_path / "local"
        _make_rez_version(local, "my_lib", "1.0.0")
        with patch.dict(os.environ, {"GTN_REZ_LOCAL_PACKAGES_PATH": str(local)}, clear=True):
            assert resolve_rez_library_json_path("my_lib") is None

    def test_python_dir_without_manifest(self, tmp_path: Path) -> None:
        local = tmp_path / "local"
        version_dir = _make_rez_version(local, "my_lib", "1.0.0")
        (version_dir / "python").mkdir()
        (version_dir / "python" / "readme.txt").write_text("")
        with patch.dict(os.environ, {"GTN_REZ_LOCAL_PACKAGES_PATH": str(local)}, clear=True):
            assert resolve_rez_library_json_path("my_lib") is None


def _library_manifest_in(folder: Path, display_name: str = "Some Display Name") -> Path:
    """Create a non-git library folder whose manifest display name differs from the folder name."""
    folder.mkdir(parents=True)
    manifest = folder / "griptape_nodes_library.json"
    manifest.write_text(json.dumps({"name": display_name}))
    return manifest


class TestLibraryRezPackageVersion:
    @pytest.fixture(autouse=True)
    def _no_git(self) -> Iterator[None]:
        with patch(f"{_RU}.get_git_repository_root", return_value=None):
            yield

    def test_no_store(self, tmp_path: Path) -> None:
        manifest = _library_manifest_in(tmp_path / "my-lib")
        with patch.dict(os.environ, {}, clear=True):
            assert get_library_rez_package_version(manifest) is None
            assert not is_library_rez_package_available(manifest)

    def test_latest_version_under_folder_family(self, tmp_path: Path) -> None:
        local = tmp_path / "store" / "local"
        _make_rez_version(local, "my_lib", "0.9.0")
        _make_rez_version(local, "my_lib", "0.10.0")
        manifest = _library_manifest_in(tmp_path / "my-lib")
        with patch.dict(os.environ, {"GTN_REZ_LOCAL_PACKAGES_PATH": str(local)}, clear=True):
            assert get_library_rez_package_version(manifest) == "0.10.0"
            assert is_library_rez_package_available(manifest)

    def test_display_name_is_never_used_as_family(self, tmp_path: Path) -> None:
        # A package named after the manifest's display name must not satisfy the lookup;
        # only the repo/folder-named package counts.
        local = tmp_path / "store" / "local"
        _make_rez_version(local, "griptape_modular_diffusion_nodes_library", "1.0.0")
        manifest = _library_manifest_in(
            tmp_path / "griptape-nodes-library-diffusers", display_name="Griptape Modular Diffusion Nodes Library"
        )
        with patch.dict(os.environ, {"GTN_REZ_LOCAL_PACKAGES_PATH": str(local)}, clear=True):
            assert get_library_rez_package_version(manifest) is None

            _make_rez_version(local, "griptape_nodes_library_diffusers", "2.0.0")
            assert get_library_rez_package_version(manifest) == "2.0.0"

    def test_family_missing(self, tmp_path: Path) -> None:
        local = tmp_path / "store" / "local"
        local.mkdir(parents=True)
        manifest = _library_manifest_in(tmp_path / "nothing-here")
        with patch.dict(os.environ, {"GTN_REZ_LOCAL_PACKAGES_PATH": str(local)}, clear=True):
            assert get_library_rez_package_version(manifest) is None

    def test_explicit_packages_root_overrides_env(self, tmp_path: Path) -> None:
        explicit_store = tmp_path / "explicit"
        _make_rez_version(explicit_store / "local", "my_lib", "2.0.0")
        env_local = tmp_path / "env" / "local"
        _make_rez_version(env_local, "my_lib", "1.0.0")
        manifest = _library_manifest_in(tmp_path / "my-lib")
        with patch.dict(os.environ, {"GTN_REZ_LOCAL_PACKAGES_PATH": str(env_local)}, clear=True):
            assert get_library_rez_package_version(manifest, packages_root=explicit_store) == "2.0.0"

    def test_explicit_packages_root_without_env(self, tmp_path: Path) -> None:
        store = tmp_path / "store"
        _make_rez_version(store / "local", "my_lib", "1.5.0")
        manifest = _library_manifest_in(tmp_path / "my-lib")
        with patch.dict(os.environ, {}, clear=True):
            assert get_library_rez_package_version(manifest, packages_root=store) == "1.5.0"

    def test_family_without_versions(self, tmp_path: Path) -> None:
        local = tmp_path / "store" / "local"
        (local / "my_lib" / "empty").mkdir(parents=True)
        manifest = _library_manifest_in(tmp_path / "my-lib")
        with patch.dict(os.environ, {"GTN_REZ_LOCAL_PACKAGES_PATH": str(local)}, clear=True):
            assert get_library_rez_package_version(manifest) is None


# ---------------------------------------------------------------------------
# Family naming via git
# ---------------------------------------------------------------------------


class TestLibraryFamilyFromGit:
    def test_uses_git_remote_name(self, tmp_path: Path) -> None:
        manifest = tmp_path / "clone123" / "griptape_nodes_library.json"
        with (
            patch(f"{_RU}.get_git_repository_root", return_value=tmp_path / "clone123"),
            patch(f"{_RU}._git_remote_repo_name", return_value="Griptape-Nodes-Library.Diffusers"),
        ):
            assert library_file_path_to_rez_family(manifest) == "griptape_nodes_library_diffusers"

    def test_uses_git_root_without_remote(self, tmp_path: Path) -> None:
        manifest = tmp_path / "my-library" / "luma" / "griptape_nodes_library.json"
        with (
            patch(f"{_RU}.get_git_repository_root", return_value=tmp_path / "my-library"),
            patch(f"{_RU}._git_remote_repo_name", return_value=None),
        ):
            assert library_file_path_to_rez_family(manifest) == "my_library"

    def test_git_error_falls_back_to_parent(self, tmp_path: Path) -> None:
        manifest = tmp_path / "Parent Dir" / "griptape_nodes_library.json"
        with patch(f"{_RU}.get_git_repository_root", side_effect=OSError("no git")):
            assert library_file_path_to_rez_family(manifest) == "parent_dir"


class TestGitRemoteRepoName:
    def test_https_url(self, tmp_path: Path) -> None:
        with patch(f"{_RU}.subprocess.run", return_value=_completed(stdout="https://github.com/org/my-repo.git\n")):
            assert _git_remote_repo_name(tmp_path) == "my-repo"

    def test_ssh_url_with_trailing_slash(self, tmp_path: Path) -> None:
        with patch(f"{_RU}.subprocess.run", return_value=_completed(stdout="git@github.com:org/other/\n")):
            assert _git_remote_repo_name(tmp_path) == "other"

    def test_no_remote(self, tmp_path: Path) -> None:
        with patch(f"{_RU}.subprocess.run", return_value=_completed(returncode=2)):
            assert _git_remote_repo_name(tmp_path) is None


# ---------------------------------------------------------------------------
# Library version derivation
# ---------------------------------------------------------------------------


class TestReadPyprojectVersion:
    def test_project_version(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "x"\nversion = "1.2.3"\n')
        assert _read_pyproject_version(pyproject) == "1.2.3"

    def test_poetry_version(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[tool.poetry]\nversion = "4.5.6"\n')
        assert _read_pyproject_version(pyproject) == "4.5.6"

    def test_no_version(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "x"\n')
        assert _read_pyproject_version(pyproject) is None

    def test_invalid_toml(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("not = [valid")
        assert _read_pyproject_version(pyproject) is None

    def test_missing_file(self, tmp_path: Path) -> None:
        assert _read_pyproject_version(tmp_path / "missing.toml") is None


class TestGitDescribeVersion:
    def test_exact_tag(self, tmp_path: Path) -> None:
        with patch(f"{_RU}.subprocess.run", return_value=_completed(stdout="v1.4.0-0-gabc1234\n")):
            assert _git_describe_version(tmp_path) == "1.4.0"

    def test_commits_ahead(self, tmp_path: Path) -> None:
        with patch(f"{_RU}.subprocess.run", return_value=_completed(stdout="1.4.0-7-gabc1234\n")):
            assert _git_describe_version(tmp_path) == "1.4.0.post7"

    def test_unexpected_format_returned_raw(self, tmp_path: Path) -> None:
        with patch(f"{_RU}.subprocess.run", return_value=_completed(stdout="v2\n")):
            assert _git_describe_version(tmp_path) == "2"

    def test_no_tags(self, tmp_path: Path) -> None:
        with patch(f"{_RU}.subprocess.run", return_value=_completed(returncode=128)):
            assert _git_describe_version(tmp_path) is None


class TestDeriveLibraryVersion:
    def test_pyproject_in_parent(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.7.1"\n')
        manifest = tmp_path / "pkg" / "griptape_nodes_library.json"
        manifest.parent.mkdir()
        assert _derive_library_version(manifest) == "0.7.1"

    def test_git_describe_fallback(self, tmp_path: Path) -> None:
        manifest = tmp_path / "griptape_nodes_library.json"
        with (
            patch(f"{_RU}._read_pyproject_version", return_value=None),
            patch(f"{_RU}._git_describe_version", return_value="2.0.0.post1"),
        ):
            assert _derive_library_version(manifest) == "2.0.0.post1"

    def test_default_version(self, tmp_path: Path) -> None:
        manifest = tmp_path / "griptape_nodes_library.json"
        with (
            patch(f"{_RU}._read_pyproject_version", return_value=None),
            patch(f"{_RU}._git_describe_version", return_value=None),
        ):
            assert _derive_library_version(manifest) == "1.0.0"


# ---------------------------------------------------------------------------
# Dependency resolution helpers
# ---------------------------------------------------------------------------


class TestBuildDirectRequires:
    def test_empty(self) -> None:
        assert build_direct_requires([]) == []

    def test_filters_to_direct_deps(self) -> None:
        resolved = [
            ResolvedPackage(pip_name="Pillow", version="10.0.0"),
            ResolvedPackage(pip_name="numpy", version="2.1.0"),
            ResolvedPackage(pip_name="ruamel.yaml", version="0.18.0"),
        ]
        with patch(f"{_RU}.resolve_full", return_value=resolved):
            result = build_direct_requires(["pillow>=10", "ruamel-yaml"])
        assert result == ["pillow-10.0.0", "ruamel_yaml-0.18.0"]


# ---------------------------------------------------------------------------
# Rez runtime probes
# ---------------------------------------------------------------------------


class TestBuildRezEnvPrefix:
    def test_prefix(self) -> None:
        with patch(f"{_RU}._rez_executable", return_value="/opt/rez/bin/rez"):
            assert build_rez_env_prefix(["lib_a", "lib_b-1.0"]) == [
                "/opt/rez/bin/rez",
                "env",
                "lib_a",
                "lib_b-1.0",
                "--",
            ]


class TestResolveAndLogRezContext:
    def test_success(self) -> None:
        with (
            patch(f"{_RU}._rez_executable", return_value="rez"),
            patch(f"{_RU}.subprocess.run", return_value=_completed(stdout="lib_a-1.0\n\npython-3.12\n")) as run,
        ):
            assert resolve_and_log_rez_context(["lib_a"]) == ["lib_a-1.0", "python-3.12"]
        cmd = run.call_args.args[0]
        assert cmd[:4] == ["rez", "env", "lib_a", "--"]

    def test_non_zero_exit(self) -> None:
        with (
            patch(f"{_RU}._rez_executable", return_value="rez"),
            patch(f"{_RU}.subprocess.run", return_value=_completed(returncode=1, stderr="resolve failed")),
        ):
            assert resolve_and_log_rez_context(["lib_a"]) == []

    def test_os_error(self) -> None:
        with (
            patch(f"{_RU}._rez_executable", return_value="rez"),
            patch(f"{_RU}.subprocess.run", side_effect=OSError("not found")),
        ):
            assert resolve_and_log_rez_context(["lib_a"]) == []

    def test_logs_path_map_when_set(self) -> None:
        with (
            patch.dict(os.environ, {"GTN_REZ_PATH_MAP": "linux=/mnt/p;windows=P:"}, clear=True),
            patch(f"{_RU}._rez_executable", return_value="rez"),
            patch(f"{_RU}.subprocess.run", return_value=_completed(stdout="")),
        ):
            assert resolve_and_log_rez_context(["lib_a"]) == []


class TestResolveRezPythonpath:
    def test_success(self) -> None:
        stdout = "/store/a/python\n/store/b/python\n"
        with (
            patch(f"{_RU}._rez_executable", return_value="rez"),
            patch(f"{_RU}.subprocess.run", return_value=_completed(stdout=stdout)),
        ):
            assert resolve_rez_pythonpath(["lib_a"]) == ["/store/a/python", "/store/b/python"]

    def test_non_zero_exit(self) -> None:
        with (
            patch(f"{_RU}._rez_executable", return_value="rez"),
            patch(f"{_RU}.subprocess.run", return_value=_completed(returncode=3, stderr="boom")),
        ):
            assert resolve_rez_pythonpath(["lib_a"]) == []

    def test_timeout(self) -> None:
        with (
            patch(f"{_RU}._rez_executable", return_value="rez"),
            patch(f"{_RU}.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="rez", timeout=30)),
        ):
            assert resolve_rez_pythonpath(["lib_a"]) == []


class TestCheckRezHealth:
    def test_healthy_sets_config_file(self, tmp_path: Path) -> None:
        config = tmp_path / "rezconfig.py"
        with (
            patch.dict(os.environ, {"GTN_REZ_CONFIG_FILE": str(config)}, clear=True),
            patch(f"{_RU}._rez_executable", return_value="rez-search"),
            patch(f"{_RU}.subprocess.run", return_value=_completed(stdout="python\nplatform\n")) as run,
        ):
            assert check_rez_health() is True
        assert run.call_args.kwargs["env"]["REZ_CONFIG_FILE"] == str(config)

    def test_healthy_debug_lists_families(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger=_RU)
        with (
            patch(f"{_RU}._rez_executable", return_value="rez-search"),
            patch(f"{_RU}.subprocess.run", return_value=_completed(stdout="zlib\nabc\n")),
        ):
            assert check_rez_health() is True
        assert "zlib" in caplog.text

    def test_unhealthy(self) -> None:
        with (
            patch(f"{_RU}._rez_executable", return_value="rez-search"),
            patch(f"{_RU}.subprocess.run", return_value=_completed(returncode=1, stderr="line1\nline2")),
        ):
            assert check_rez_health() is False


class TestCheckRezHealthDetailed:
    def test_healthy(self, tmp_path: Path) -> None:
        with (
            patch.dict(os.environ, {"GTN_REZ_CONFIG_FILE": str(tmp_path / "rezconfig.py")}, clear=True),
            patch(f"{_RU}._rez_executable", return_value="rez-search"),
            patch(f"{_RU}.subprocess.run", return_value=_completed(stdout="a\nb\n\nc\n")),
        ):
            result = check_rez_health_detailed()
        assert result.healthy is True
        assert result.package_count == 3  # noqa: PLR2004
        assert result.check_duration_ms >= 0
        assert result.timestamp

    def test_non_zero_exit(self) -> None:
        with (
            patch(f"{_RU}._rez_executable", return_value="rez-search"),
            patch(f"{_RU}.subprocess.run", return_value=_completed(returncode=1)),
        ):
            result = check_rez_health_detailed()
        assert result.healthy is False
        assert result.package_count == 0

    def test_os_error(self) -> None:
        with (
            patch(f"{_RU}._rez_executable", return_value="rez-search"),
            patch(f"{_RU}.subprocess.run", side_effect=OSError("missing binary")),
        ):
            result = check_rez_health_detailed()
        assert result.healthy is False


class TestRezContextString:
    def test_reads_used_request(self) -> None:
        with patch.dict(os.environ, {"REZ_USED_REQUEST": "griptape_launch"}, clear=True):
            assert get_rez_context_string() == "griptape_launch"

    def test_empty_outside_rez(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert get_rez_context_string() == ""


# ---------------------------------------------------------------------------
# Library package building
# ---------------------------------------------------------------------------


def _make_library_source(root: Path) -> Path:
    """Create a library checkout with a manifest, code, and dirs that must not be copied."""
    root.mkdir(parents=True)
    manifest = root / "griptape_nodes_library.json"
    manifest.write_text(json.dumps({"name": "My Library"}))
    (root / "nodes.py").write_text("# nodes\n")
    (root / "pyproject.toml").write_text('[project]\nversion = "2.3.4"\n')
    for excluded in (".venv", ".venv-exec", ".git", "__pycache__"):
        (root / excluded).mkdir()
        (root / excluded / "marker").write_text("")
    return manifest


class TestWriteLibraryMetaPackage:
    def test_writes_package_and_copies_source(self, tmp_path: Path) -> None:
        manifest = _make_library_source(tmp_path / "src" / "my-library")
        store = tmp_path / "store"

        _write_library_meta_package(
            "My Library",
            "2.3.4",
            ["torch-2.7.0", "pillow-10.0.0"],
            store,
            rez_family="my_library",
            library_source_dir=manifest.parent,
            library_json_name=manifest.name,
        )

        version_dir = store / "local" / "my_library" / "2.3.4"
        content = (version_dir / "package.py").read_text()
        assert "name = 'my_library'" in content
        assert "version = '2.3.4'" in content
        assert content.index("'pillow-10.0.0'") < content.index("'torch-2.7.0'")
        assert "env.PYTHONPATH.append('{root}/python')" in content
        assert "env.GTN_REZ_LIBRARY_JSON = '{root}/python/griptape_nodes_library.json'" in content
        assert "format_version = 2" in content

        python_dir = version_dir / "python"
        assert (python_dir / "nodes.py").is_file()
        assert (python_dir / "griptape_nodes_library.json").is_file()
        for excluded in (".venv", ".venv-exec", ".git", "__pycache__"):
            assert not (python_dir / excluded).exists()

    def test_skip_installed_keeps_existing(self, tmp_path: Path) -> None:
        manifest = _make_library_source(tmp_path / "src" / "lib")
        store = tmp_path / "store"
        pkg_file = store / "local" / "fam" / "1.0.0" / "package.py"
        pkg_file.parent.mkdir(parents=True)
        pkg_file.write_text("original")
        source = {
            "rez_family": "fam",
            "library_source_dir": manifest.parent,
            "library_json_name": manifest.name,
        }

        _write_library_meta_package("Lib", "1.0.0", ["a-1"], store, **source, skip_installed=True)
        assert pkg_file.read_text() == "original"

        _write_library_meta_package("Lib", "1.0.0", ["a-1"], store, **source, skip_installed=False)
        assert "'a-1'" in pkg_file.read_text()


class TestInstallLibraryAsRezPackage:
    def test_no_dependencies_writes_package_only(self, tmp_path: Path) -> None:
        manifest = _make_library_source(tmp_path / "src" / "my-library")
        local = tmp_path / "store" / "local"
        with (
            patch.dict(os.environ, {"GTN_REZ_LOCAL_PACKAGES_PATH": str(local)}, clear=True),
            patch(f"{_RU}.resolve_full") as resolve,
            patch(f"{_RU}.rez_uv_install") as install,
            patch(f"{_RU}.get_git_repository_root", return_value=None),
        ):
            install_library_as_rez_package("My Library", [], library_file_path=manifest)

        resolve.assert_not_called()
        install.assert_not_called()
        package_py = local / "my_library" / "2.3.4" / "package.py"
        namespace: dict = {}
        exec(compile(package_py.read_text(), str(package_py), "exec"), namespace)  # noqa: S102
        assert namespace["name"] == "my_library"
        assert namespace["requires"] == []
        assert callable(namespace["commands"])
        assert (package_py.parent / "python" / "nodes.py").is_file()
        assert (package_py.parent / "python" / "griptape_nodes_library.json").is_file()

    def test_failed_dependency_install_writes_no_library_package(self, tmp_path: Path) -> None:
        manifest = _make_library_source(tmp_path / "src" / "my-library")
        local = tmp_path / "store" / "local"
        with (
            patch.dict(os.environ, {"GTN_REZ_LOCAL_PACKAGES_PATH": str(local)}, clear=True),
            patch(f"{_RU}.resolve_full", return_value=[ResolvedPackage(pip_name="torch", version="2.7.0")]),
            patch(f"{_RU}.rez_uv_install", side_effect=RezInstallError(["torch==2.7.0 (download failed: x)"])),
            patch(f"{_RU}.get_git_repository_root", return_value=None),
            pytest.raises(RezInstallError),
        ):
            install_library_as_rez_package("My Library", ["torch==2.7.0"], library_file_path=manifest)

        assert not (local / "my_library").exists()

    def test_no_store_configured(self, tmp_path: Path) -> None:
        manifest = _make_library_source(tmp_path / "src" / "lib")
        with patch.dict(os.environ, {}, clear=True), patch(f"{_RU}.rez_uv_install") as install:
            install_library_as_rez_package("Lib", ["requests"], library_file_path=manifest)
        install.assert_not_called()

    def test_explicit_packages_root_used_without_env(self, tmp_path: Path) -> None:
        manifest = _make_library_source(tmp_path / "src" / "lib")
        store = tmp_path / "store"
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(f"{_RU}.resolve_full", return_value=[ResolvedPackage(pip_name="requests", version="2.32.0")]),
            patch(f"{_RU}.rez_uv_install") as install,
            patch(f"{_RU}.get_git_repository_root", return_value=None),
        ):
            install_library_as_rez_package("Lib", ["requests"], library_file_path=manifest, packages_root=store)
            assert "GTN_REZ_LOCAL_PACKAGES_PATH" not in os.environ

        assert install.call_args.kwargs["packages_dir"] == store
        assert (store / "local" / "lib" / "2.3.4" / "package.py").is_file()

    def test_installs_edit_and_exec_deps(self, tmp_path: Path) -> None:
        manifest = _make_library_source(tmp_path / "src" / "my-library")
        local = tmp_path / "store" / "local"
        resolved = [
            ResolvedPackage(pip_name="Pillow", version="10.0.0"),
            ResolvedPackage(pip_name="torch", version="2.7.0"),
            ResolvedPackage(pip_name="numpy", version="2.1.0"),
        ]
        with (
            patch.dict(os.environ, {"GTN_REZ_LOCAL_PACKAGES_PATH": str(local)}, clear=True),
            patch(f"{_RU}.resolve_full", return_value=resolved) as resolve,
            patch(f"{_RU}.rez_uv_install") as install,
            patch(f"{_RU}.get_git_repository_root", return_value=None),
        ):
            install_library_as_rez_package(
                "My Library",
                ["pillow>=10"],
                pip_dependencies_exec=["torch==2.7.0"],
                library_file_path=manifest,
                extra_index_url="https://example.invalid/simple",
                pip_install_flags=["--torch-backend=auto"],
                python_version="3.12",
                skip_installed=False,
            )

        assert resolve.call_args.args[0] == ["pillow>=10", "torch==2.7.0"]
        assert resolve.call_args.kwargs["extra_flags"] == ["--torch-backend=auto"]
        install_kwargs = install.call_args.kwargs
        assert install.call_args.args[0] == ["pillow>=10", "torch==2.7.0"]
        assert install_kwargs["packages_dir"] == local.parent
        assert install_kwargs["extra_index_url"] == "https://example.invalid/simple"
        assert install_kwargs["python_version"] == "3.12"
        assert install_kwargs["skip_installed"] is False

        package_py = local / "my_library" / "2.3.4" / "package.py"
        content = package_py.read_text()
        assert "'pillow-10.0.0'" in content
        assert "'torch-2.7.0'" in content
        assert "numpy" not in content
        assert (package_py.parent / "python" / "nodes.py").is_file()

    def test_package_named_after_folder_not_display_name(self, tmp_path: Path) -> None:
        # The manifest's display name is "My Library"; the package must be named after
        # the folder the library was built from.
        manifest = _make_library_source(tmp_path / "src" / "studio-tools")
        local = tmp_path / "store" / "local"
        with (
            patch.dict(os.environ, {"GTN_REZ_LOCAL_PACKAGES_PATH": str(local)}, clear=True),
            patch(f"{_RU}.resolve_full", return_value=[ResolvedPackage(pip_name="requests", version="2.32.0")]),
            patch(f"{_RU}.rez_uv_install"),
            patch(f"{_RU}.get_git_repository_root", return_value=None),
        ):
            install_library_as_rez_package("My Library", ["requests"], library_file_path=manifest)

        content = (local / "studio_tools" / "2.3.4" / "package.py").read_text()
        assert "name = 'studio_tools'" in content
        assert "'requests-2.32.0'" in content
        assert not (local / "my_library").exists()


# ---------------------------------------------------------------------------
# Pinned requires, rez context, and dependency ref pins
# ---------------------------------------------------------------------------


def _write_library_package(store_local: Path, family: str, version: str, package_body: str) -> Path:
    """Create a store library package and return the manifest inside its python/ dir."""
    version_dir = store_local / family / version
    (version_dir / "python").mkdir(parents=True)
    (version_dir / "package.py").write_text(package_body)
    manifest = version_dir / "python" / "griptape_nodes_library.json"
    manifest.write_text(json.dumps({"name": "Display Name"}))
    return manifest


class TestReadLibraryPackageRequires:
    def test_reads_requires_from_store_package(self, tmp_path: Path) -> None:
        manifest = _write_library_package(
            tmp_path / "local",
            "my_lib",
            "1.0.0",
            "name = 'my_lib'\nrequires = [\n    'torch-2.7.0',\n    'pillow-10.0.0',\n]\n",
        )
        assert read_library_package_requires(manifest) == ["torch-2.7.0", "pillow-10.0.0"]

    def test_local_checkout_reads_latest_store_package(self, tmp_path: Path) -> None:
        local = tmp_path / "store" / "local"
        _write_library_package(local, "my_lib", "1.0.0", "requires = ['torch-2.6.0']\n")
        _write_library_package(local, "my_lib", "1.1.0", "requires = ['torch-2.7.0']\n")
        checkout = tmp_path / "my-lib"
        checkout.mkdir()
        manifest = checkout / "griptape_nodes_library.json"
        manifest.write_text("{}")
        with (
            patch.dict(os.environ, {"GTN_REZ_LOCAL_PACKAGES_PATH": str(local)}, clear=True),
            patch(f"{_RU}.get_git_repository_root", return_value=None),
        ):
            assert read_library_package_requires(manifest) == ["torch-2.7.0"]

    def test_no_package_returns_empty(self, tmp_path: Path) -> None:
        manifest = tmp_path / "loose" / "griptape_nodes_library.json"
        manifest.parent.mkdir()
        manifest.write_text("{}")
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(f"{_RU}.get_git_repository_root", return_value=None),
        ):
            assert read_library_package_requires(manifest) == []

    @pytest.mark.parametrize(
        "package_body",
        [
            "name = 'my_lib'\n",
            "requires = not valid python (\n",
            "requires = build_requires()\n",
            "requires = 'torch-2.7.0'\n",
        ],
    )
    def test_unreadable_requires_returns_empty(self, tmp_path: Path, package_body: str) -> None:
        manifest = _write_library_package(tmp_path / "local", "my_lib", "1.0.0", package_body)
        assert read_library_package_requires(manifest) == []

    def test_package_is_never_executed(self, tmp_path: Path) -> None:
        marker = tmp_path / "executed"
        body = f"open({str(marker)!r}, 'w').write('x')\nrequires = ['torch-2.7.0']\n"
        manifest = _write_library_package(tmp_path / "local", "my_lib", "1.0.0", body)
        assert read_library_package_requires(manifest) == ["torch-2.7.0"]
        assert not marker.exists()


class TestLibraryEditRezRequests:
    def test_pins_edit_deps_from_package_requires(self, tmp_path: Path) -> None:
        manifest = _write_library_package(
            tmp_path / "local",
            "my_lib",
            "1.0.0",
            "requires = ['pillow-10.0.0', 'ruamel_yaml-0.18.6', 'torch-2.7.0']\n",
        )
        requests = library_edit_rez_requests(manifest, ["Pillow>=10", "ruamel.yaml"])
        assert requests == ["pillow-10.0.0", "ruamel_yaml-0.18.6"]

    def test_unpinned_dependency_falls_back_to_family(self, tmp_path: Path) -> None:
        manifest = _write_library_package(tmp_path / "local", "my_lib", "1.0.0", "requires = ['pillow-10.0.0']\n")
        assert library_edit_rez_requests(manifest, ["pillow", "numpy<3"]) == ["pillow-10.0.0", "numpy"]


class TestIsInRezContext:
    def test_family_in_resolve(self) -> None:
        with patch.dict(os.environ, {"REZ_USED_RESOLVE": "python-3.12.4 my_lib-1.0.0 torch-2.7.0"}):
            assert is_in_rez_context("my_lib")

    def test_family_not_in_resolve(self) -> None:
        with patch.dict(os.environ, {"REZ_USED_RESOLVE": "python-3.12.4 griptape_launch-1.0"}):
            assert not is_in_rez_context("my_lib")

    def test_prefix_of_another_family_does_not_match(self) -> None:
        with patch.dict(os.environ, {"REZ_USED_RESOLVE": "my_lib_extra-1.0.0"}):
            assert not is_in_rez_context("my_lib")

    def test_outside_rez(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert not is_in_rez_context("my_lib")


class TestRezVersionFromGitRef:
    @pytest.mark.parametrize(
        ("ref", "expected"),
        [
            (None, None),
            ("", None),
            ("1.2.0", "1.2.0"),
            ("v1.2.0", "1.2.0"),
            ("V2.0", "2.0"),
            ("1.2.0rc1", "1.2.0rc1"),
            ("main", None),
            ("feature/rez", None),
            ("3f9c2e1", None),
            ("1", None),
        ],
    )
    def test_ref_to_version(self, ref: str | None, expected: str | None) -> None:
        assert rez_version_from_git_ref(ref) == expected


# ---------------------------------------------------------------------------
# Rez configuration: opt-in layering, search path check, local + release lookups
# ---------------------------------------------------------------------------


class TestRezSubprocessEnv:
    def test_inherits_environment_unchanged_without_opt_in(self) -> None:
        with patch.dict(os.environ, {"REZ_CONFIG_FILE": "/studio/rezconfig.py", "OTHER": "x"}, clear=True):
            env = rez_subprocess_env()
        assert env == {"REZ_CONFIG_FILE": "/studio/rezconfig.py", "OTHER": "x"}

    def test_opt_in_config_used_when_no_studio_config(self, tmp_path: Path) -> None:
        config = tmp_path / "griptape_rezconfig.py"
        with patch.dict(os.environ, {"GTN_REZ_CONFIG_FILE": str(config)}, clear=True):
            env = rez_subprocess_env()
        assert env["REZ_CONFIG_FILE"] == str(config)

    def test_opt_in_config_is_layered_after_studio_config(self, tmp_path: Path) -> None:
        config = tmp_path / "griptape_rezconfig.py"
        environ = {"REZ_CONFIG_FILE": "/studio/rezconfig.py", "GTN_REZ_CONFIG_FILE": str(config)}
        with patch.dict(os.environ, environ, clear=True):
            env = rez_subprocess_env()
        assert env["REZ_CONFIG_FILE"] == os.pathsep.join(["/studio/rezconfig.py", str(config)])

    def test_opt_in_config_not_added_twice(self, tmp_path: Path) -> None:
        config = tmp_path / "griptape_rezconfig.py"
        environ = {
            "REZ_CONFIG_FILE": os.pathsep.join(["/studio/rezconfig.py", str(config)]),
            "GTN_REZ_CONFIG_FILE": str(config),
        }
        with patch.dict(os.environ, environ, clear=True):
            env = rez_subprocess_env()
        assert env["REZ_CONFIG_FILE"] == environ["REZ_CONFIG_FILE"]

    def test_does_not_modify_the_process_environment(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"GTN_REZ_CONFIG_FILE": str(tmp_path / "c.py")}, clear=True):
            rez_subprocess_env()
            assert "REZ_CONFIG_FILE" not in os.environ


class TestRezSearchPaths:
    def test_reads_packages_path_as_json(self, tmp_path: Path) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout='["/a", "/b"]\n', stderr="")
        config = tmp_path / "c.py"
        with (
            patch.dict(os.environ, {"GTN_REZ_CONFIG_FILE": str(config)}, clear=True),
            patch(f"{_RU}._rez_executable", return_value="rez-config"),
            patch(f"{_RU}.subprocess.run", return_value=completed) as run,
        ):
            assert rez_search_paths() == [Path("/a"), Path("/b")]
        assert run.call_args.args[0] == ["rez-config", "--json", "packages_path"]
        assert run.call_args.kwargs["env"]["REZ_CONFIG_FILE"] == str(config)

    @pytest.mark.parametrize(
        "outcome",
        [
            subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom"),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr=""),
        ],
    )
    def test_unreadable_answer_returns_none(self, outcome: subprocess.CompletedProcess[str]) -> None:
        with (
            patch(f"{_RU}._rez_executable", return_value="rez-config"),
            patch(f"{_RU}.subprocess.run", return_value=outcome),
        ):
            assert rez_search_paths() is None

    def test_missing_rez_returns_none(self) -> None:
        with (
            patch(f"{_RU}._rez_executable", return_value="rez-config"),
            patch(f"{_RU}.subprocess.run", side_effect=FileNotFoundError("rez-config")),
        ):
            assert rez_search_paths() is None


class TestRezUnsearchedStores:
    def test_reports_stores_rez_does_not_search(self, tmp_path: Path) -> None:
        local = tmp_path / "local"
        release = tmp_path / "release"
        environ = {"GTN_REZ_LOCAL_PACKAGES_PATH": str(local), "GTN_REZ_RELEASE_PACKAGES_PATH": str(release)}
        with (
            patch.dict(os.environ, environ, clear=True),
            patch(f"{_RU}.rez_search_paths", return_value=[local, tmp_path / "studio"]),
        ):
            assert rez_unsearched_stores() == [release]

    def test_all_stores_searched(self, tmp_path: Path) -> None:
        local = tmp_path / "local"
        with (
            patch.dict(os.environ, {"GTN_REZ_LOCAL_PACKAGES_PATH": str(local)}, clear=True),
            patch(f"{_RU}.rez_search_paths", return_value=[local]),
        ):
            assert rez_unsearched_stores() == []

    def test_different_spellings_of_the_same_store_match(self, tmp_path: Path) -> None:
        local = tmp_path / "local"
        local.mkdir()
        with patch(f"{_RU}.rez_search_paths", return_value=[tmp_path / "x" / ".." / "local"]):
            assert rez_unsearched_stores([local]) == []

    def test_unknown_search_path_reports_nothing(self, tmp_path: Path) -> None:
        with patch(f"{_RU}.rez_search_paths", return_value=None):
            assert rez_unsearched_stores([tmp_path / "local"]) == []

    def test_no_stores_does_not_ask_rez(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch(f"{_RU}.rez_search_paths") as search:
            assert rez_unsearched_stores() == []
        search.assert_not_called()


class TestLocalAndReleaseStoreLookups:
    @pytest.fixture(autouse=True)
    def _no_git(self) -> Iterator[None]:
        with patch(f"{_RU}.get_git_repository_root", return_value=None):
            yield

    def _stores(self, tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
        local = tmp_path / "store" / "local"
        release = tmp_path / "store" / "release"
        environ = {"GTN_REZ_LOCAL_PACKAGES_PATH": str(local), "GTN_REZ_RELEASE_PACKAGES_PATH": str(release)}
        return local, release, environ

    def test_released_package_is_found(self, tmp_path: Path) -> None:
        _, release, environ = self._stores(tmp_path)
        _make_rez_version(release, "my_lib", "1.0.0", manifest="griptape_nodes_library.json")
        manifest = _library_manifest_in(tmp_path / "my-lib")
        with patch.dict(os.environ, environ, clear=True):
            assert get_library_rez_package_version(manifest) == "1.0.0"
            assert resolve_rez_library_json_path("my_lib") == release / "my_lib" / "1.0.0" / "python" / (
                "griptape_nodes_library.json"
            )

    def test_highest_version_across_stores_wins(self, tmp_path: Path) -> None:
        local, release, environ = self._stores(tmp_path)
        _make_rez_version(local, "my_lib", "1.0.0", manifest="griptape_nodes_library.json")
        _make_rez_version(release, "my_lib", "1.2.0", manifest="griptape_nodes_library.json")
        manifest = _library_manifest_in(tmp_path / "my-lib")
        with patch.dict(os.environ, environ, clear=True):
            assert get_library_rez_package_version(manifest) == "1.2.0"
            json_path = resolve_rez_library_json_path("my_lib")
        assert json_path is not None
        assert json_path.is_relative_to(release)

    def test_same_version_in_both_prefers_local(self, tmp_path: Path) -> None:
        local, release, environ = self._stores(tmp_path)
        _make_rez_version(local, "my_lib", "1.0.0", manifest="griptape_nodes_library.json")
        _make_rez_version(release, "my_lib", "1.0.0", manifest="griptape_nodes_library.json")
        with patch.dict(os.environ, environ, clear=True):
            json_path = resolve_rez_library_json_path("my_lib")
        assert json_path is not None
        assert json_path.is_relative_to(local)

    def test_pinned_version_found_in_release(self, tmp_path: Path) -> None:
        local, release, environ = self._stores(tmp_path)
        _make_rez_version(local, "my_lib", "2.0.0", manifest="griptape_nodes_library.json")
        _make_rez_version(release, "my_lib", "1.0.0", manifest="griptape_nodes_library.json")
        with patch.dict(os.environ, environ, clear=True):
            json_path = resolve_rez_library_json_path("my_lib", version="1.0.0")
            missing = resolve_rez_library_json_path("my_lib", version="3.0.0")
        assert json_path == release / "my_lib" / "1.0.0" / "python" / "griptape_nodes_library.json"
        assert missing is None

    def test_local_store_folder_need_not_be_named_local(self, tmp_path: Path) -> None:
        store = tmp_path / "studio" / "griptape_builds"
        _make_rez_version(store, "my_lib", "1.0.0", manifest="griptape_nodes_library.json")
        manifest = _library_manifest_in(tmp_path / "my-lib")
        with patch.dict(os.environ, {"GTN_REZ_LOCAL_PACKAGES_PATH": str(store)}, clear=True):
            assert get_library_rez_package_version(manifest) == "1.0.0"
            assert resolve_rez_library_json_path("my_lib") is not None

    def test_pinned_requires_read_from_released_package(self, tmp_path: Path) -> None:
        _, release, environ = self._stores(tmp_path)
        version_dir = _make_rez_version(release, "my_lib", "1.0.0")
        (version_dir / "package.py").write_text("requires = ['pillow-10.0.0']\n")
        manifest = _library_manifest_in(tmp_path / "my-lib")
        with patch.dict(os.environ, environ, clear=True):
            assert library_edit_rez_requests(manifest, ["pillow"]) == ["pillow-10.0.0"]
