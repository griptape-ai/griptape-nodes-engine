"""Unit tests for rez_utils module."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

from griptape_nodes.utils.rez_utils import (
    _detect_rez_store_family,
    _library_rez_name,
    _parse_rez_spec,
    _resolve_rez_path,
    current_platform_key,
    find_library_manifest,
    is_rez_enabled,
    is_rez_library_path,
    library_file_path_to_rez_family,
    list_available_library_packages,
    pip_spec_name,
    read_library_dependencies,
    read_library_manifest,
    rez_library_package_name,
    rez_library_package_version,
    rez_path_map,
    rez_root,
)

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
# Package store browsing
# ---------------------------------------------------------------------------


class TestListAvailableLibraryPackages:
    def test_lists_library_packages(self, tmp_path: Path) -> None:
        local_dir = tmp_path / "local"
        for name, ver in [("griptape_nodes_library_standard", "0.81.0"), ("griptape_nodes_library_diffusers", "0.5.0")]:
            pkg_dir = local_dir / name / ver
            pkg_dir.mkdir(parents=True)
            (pkg_dir / "package.py").write_text(f"name = '{name}'")

        # Also create a non-library package (should be excluded)
        torch_dir = local_dir / "torch" / "2.7.0"
        torch_dir.mkdir(parents=True)
        (torch_dir / "package.py").write_text("name = 'torch'")

        with patch.dict(os.environ, {"GTN_REZ_LOCAL_PACKAGES_PATH": str(local_dir)}):
            packages = list_available_library_packages()

        assert len(packages) == 2  # noqa: PLR2004
        families = {p["family"] for p in packages}
        assert "griptape_nodes_library_standard" in families
        assert "griptape_nodes_library_diffusers" in families
        assert "torch" not in families

    def test_empty_store(self, tmp_path: Path) -> None:
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        with patch.dict(os.environ, {"GTN_REZ_LOCAL_PACKAGES_PATH": str(local_dir)}):
            assert list_available_library_packages() == []

    def test_no_packages_path(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("GTN_REZ_LOCAL_PACKAGES_PATH", None)
            assert list_available_library_packages() == []


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
