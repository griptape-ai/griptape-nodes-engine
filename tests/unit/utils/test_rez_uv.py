"""Unit tests for rez_uv module."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from griptape_nodes.utils import rez_uv
from griptape_nodes.utils.rez_uv import (
    ResolvedPackage,
    WheelInfo,
    _build_requires,
    _copy_payload,
    _detect_wheel_tag,
    _install_one,
    _marker_applies,
    _merge_variant,
    _parse_annotation_graph,
    _parse_entry_points,
    _parse_requires_dist,
    _pep440_spec_to_rez,
    _read_existing_variants,
    _variant_subpath,
    _write_package_py,
    install,
    is_installed,
    pip_normalize,
    read_wheel_info,
    resolve_full,
    rez_name,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

LINUX = ("linux", "x86_64")
OSX = ("osx", "arm64")

COMPILE_OUTPUT = """\
certifi==2024.2.2
    # via requests
idna==3.7
    # via requests
requests==2.32.3
    # via -r -
torch==2.7.0+cu128
    # via
    #   -r -
    #   torchvision
torchvision==0.22.0
    # via -r -
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_fake_wheel(  # noqa: PLR0913
    target: Path,
    name: str,
    version: str,
    *,
    requires: tuple[str, ...] = (),
    scripts: tuple[str, ...] = (),
    tag: str = "py3-none-any",
    summary: str = "A test package",
) -> None:
    """Write the directory layout ``uv pip install --target`` produces for one wheel."""
    module_name = rez_name(name)
    dist_info = target / f"{module_name}-{version}.dist-info"
    dist_info.mkdir(parents=True)
    metadata = [
        "Metadata-Version: 2.1",
        f"Name: {name}",
        f"Version: {version}",
        f"Summary: {summary}",
        "Author: Test Author",
        "Author-email: author@example.com",
        "Requires-Python: >=3.9",
        *[f"Requires-Dist: {r}" for r in requires],
    ]
    (dist_info / "METADATA").write_text("\n".join(metadata) + "\n", encoding="utf-8")
    (dist_info / "WHEEL").write_text(f"Wheel-Version: 1.0\nTag: {tag}\n", encoding="utf-8")
    module_dir = target / module_name
    module_dir.mkdir()
    (module_dir / "__init__.py").write_text("", encoding="utf-8")
    if scripts:
        entries = "".join(f"{s} = {module_name}:main\n" for s in scripts)
        (dist_info / "entry_points.txt").write_text(f"[console_scripts]\n{entries}", encoding="utf-8")
        bin_dir = target / "bin"
        bin_dir.mkdir()
        for s in scripts:
            (bin_dir / s).write_text("#!/bin/sh\n", encoding="utf-8")


def _wheel_info(**overrides: Any) -> WheelInfo:
    fields: dict[str, Any] = {
        "pip_name": "Demo-Pkg",
        "version": "1.0.0",
        "description": "Demo package",
        "authors": ["Test Author"],
        "requires_python": ">=3.9",
        "console_scripts": [],
        "is_pure_python": True,
        "platform_tag": "any",
        "requires_dist": [],
    }
    fields.update(overrides)
    return WheelInfo(**fields)


class FakeUv:
    """Stand-in for ``subprocess.run`` that emulates ``uv pip compile`` and ``uv pip install``."""

    def __init__(
        self,
        wheels: dict[str, dict[str, Any]] | None = None,
        *,
        compile_output: str = "",
        fail_install: set[str] | None = None,
    ) -> None:
        self.wheels = wheels or {}
        self.compile_output = compile_output
        self.fail_install = fail_install or set()
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        if "compile" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=self.compile_output, stderr="")
        target = Path(cmd[cmd.index("--target") + 1])
        spec = cmd[cmd.index("--target") + 2]
        name, version = spec.split("==")
        if name in self.fail_install:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="error: no matching distribution\n")
        wheel = self.wheels.get(name)
        if wheel is not None:
            _write_fake_wheel(target, name, version, **wheel)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


@pytest.fixture
def linux_platform() -> Iterator[None]:
    """Pin rez platform detection to linux/x86_64 so variant paths are deterministic."""
    with patch.object(rez_uv, "_current_rez_platform", return_value=LINUX):
        yield


@pytest.fixture
def mock_logger() -> Iterator[MagicMock]:
    """Replace the module logger so tests can assert on log calls."""
    with patch.object(rez_uv, "logger") as logger:
        yield logger


# ---------------------------------------------------------------------------
# Name normalisation
# ---------------------------------------------------------------------------


class TestNames:
    @pytest.mark.parametrize(
        ("pip_name", "expected"),
        [
            ("ruamel.yaml", "ruamel_yaml"),
            ("Typing-Extensions", "typing_extensions"),
            ("foo__bar--baz", "foo_bar_baz"),
            ("numpy", "numpy"),
        ],
    )
    def test_rez_name(self, pip_name: str, expected: str) -> None:
        assert rez_name(pip_name) == expected

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("ruamel.yaml", "ruamel-yaml"),
            ("Typing_Extensions", "typing-extensions"),
            ("a._-b", "a-b"),
        ],
    )
    def test_pip_normalize(self, name: str, expected: str) -> None:
        assert pip_normalize(name) == expected


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


class TestParseAnnotationGraph:
    def test_parses_packages_and_requirers(self) -> None:
        pkg_list, requirers_of = _parse_annotation_graph(COMPILE_OUTPUT)

        assert pkg_list == [
            ("certifi", "2024.2.2"),
            ("idna", "3.7"),
            ("requests", "2.32.3"),
            ("torch", "2.7.0"),
            ("torchvision", "0.22.0"),
        ]
        assert requirers_of["certifi"] == {"requests"}
        assert requirers_of["idna"] == {"requests"}
        assert requirers_of["torch"] == {"torchvision"}

    def test_requirement_file_lines_are_not_requirers(self) -> None:
        _, requirers_of = _parse_annotation_graph(COMPILE_OUTPUT)
        assert "requests" not in requirers_of
        assert "torchvision" not in requirers_of

    def test_requirer_names_are_normalised(self) -> None:
        stdout = "ruamel-yaml-clib==0.2.8\n    # via ruamel.yaml\nruamel-yaml==0.18.6\n"
        _, requirers_of = _parse_annotation_graph(stdout)
        assert requirers_of["ruamel-yaml-clib"] == {"ruamel-yaml"}

    def test_unrelated_line_ends_via_block(self) -> None:
        stdout = "a==1\n    # via\n    #   b\n--index-url x\n    # c\n"
        _, requirers_of = _parse_annotation_graph(stdout)
        assert requirers_of["a"] == {"b"}

    def test_empty_output(self) -> None:
        assert _parse_annotation_graph("") == ([], {})


class TestResolveFull:
    def test_builds_compile_command_and_graph(self, mock_logger: MagicMock) -> None:
        fake = FakeUv(compile_output=COMPILE_OUTPUT)
        with patch.object(rez_uv.subprocess, "run", side_effect=fake) as run:
            resolved = resolve_full(
                ["requests", "torchvision"],
                extra_index_url="https://example.com/simple",
                extra_flags=["--torch-backend=auto"],
                python_version="3.12",
                uv_cmd="uv-bin",
            )

        cmd = fake.calls[0]
        assert cmd[:4] == ["uv-bin", "pip", "compile", "--no-header"]
        assert cmd[cmd.index("--python-version") + 1] == "3.12"
        assert cmd[cmd.index("--extra-index-url") + 1] == "https://example.com/simple"
        assert "--torch-backend=auto" in cmd
        assert run.call_args.kwargs["input"] == "requests\ntorchvision\n"

        by_name = {p.pip_name: p for p in resolved}
        assert by_name["requests"].direct_deps == {"certifi", "idna"}
        assert by_name["torchvision"].direct_deps == {"torch"}
        assert by_name["torch"].version == "2.7.0"
        assert by_name["certifi"].direct_deps == set()
        mock_logger.debug.assert_called()

    def test_string_input_and_defaults(self, mock_logger: MagicMock) -> None:
        mock_logger.isEnabledFor.return_value = False
        fake = FakeUv(compile_output="six==1.16.0\n    # via -r -\n")
        with (
            patch.object(rez_uv.subprocess, "run", side_effect=fake) as run,
            patch.object(rez_uv, "find_uv_bin", return_value="found-uv"),
        ):
            resolved = resolve_full("six")

        cmd = fake.calls[0]
        assert cmd[0] == "found-uv"
        assert cmd[cmd.index("--python-version") + 1] == f"{sys.version_info.major}.{sys.version_info.minor}"
        assert "--extra-index-url" not in cmd
        assert run.call_args.kwargs["input"] == "six\n"
        assert resolved == [ResolvedPackage(pip_name="six", version="1.16.0", direct_deps=set())]


# ---------------------------------------------------------------------------
# Wheel metadata
# ---------------------------------------------------------------------------


class TestParseEntryPoints:
    def test_reads_only_console_scripts(self) -> None:
        text = (
            "[gui_scripts]\ngui = pkg:gui\n\n[console_scripts]\ncli = pkg:main\ntool=pkg.tool:run\n\n[other]\nx = y\n"
        )
        assert _parse_entry_points(text) == ["cli", "tool"]

    def test_no_console_scripts(self) -> None:
        assert _parse_entry_points("[other]\nx = y\n") == []


class TestDetectWheelTag:
    def test_pure_python(self, tmp_path: Path) -> None:
        (tmp_path / "WHEEL").write_text("Wheel-Version: 1.0\nTag: py3-none-any\n", encoding="utf-8")
        assert _detect_wheel_tag(tmp_path) == (True, "any")

    def test_platform_wheel(self, tmp_path: Path) -> None:
        (tmp_path / "WHEEL").write_text("Tag: cp312-cp312-macosx_11_0_arm64\n", encoding="utf-8")
        assert _detect_wheel_tag(tmp_path) == (False, "macosx_11_0_arm64")

    def test_missing_wheel_file(self, tmp_path: Path) -> None:
        assert _detect_wheel_tag(tmp_path) == (True, "any")

    def test_malformed_tag_defaults_to_pure(self, tmp_path: Path) -> None:
        (tmp_path / "WHEEL").write_text("Wheel-Version: 1.0\nTag: py3-none\n", encoding="utf-8")
        assert _detect_wheel_tag(tmp_path) == (True, "any")


class TestReadWheelInfo:
    def test_reads_metadata(self, tmp_path: Path, mock_logger: MagicMock) -> None:  # noqa: ARG002
        _write_fake_wheel(
            tmp_path,
            "Demo-Pkg",
            "1.2.3",
            requires=("requests>=2", 'pywin32; sys_platform == "win32"'),
            scripts=("demo",),
            tag="cp312-cp312-manylinux_2_17_x86_64",
        )

        info = read_wheel_info(tmp_path, "demo-pkg", "1.2.3")

        assert info.pip_name == "Demo-Pkg"
        assert info.version == "1.2.3"
        assert info.description == "A test package"
        assert info.authors == ["author@example.com", "Test Author"]
        assert info.requires_python == ">=3.9"
        assert info.console_scripts == ["demo"]
        assert info.is_pure_python is False
        assert info.platform_tag == "manylinux_2_17_x86_64"
        assert info.requires_dist == ["requests>=2", 'pywin32; sys_platform == "win32"']

    def test_no_entry_points(self, tmp_path: Path) -> None:
        _write_fake_wheel(tmp_path, "plain", "0.1")
        info = read_wheel_info(tmp_path, "plain", "0.1")
        assert info.console_scripts == []
        assert info.is_pure_python is True

    def test_missing_dist_info_raises(self, tmp_path: Path) -> None:
        (tmp_path / "some_module").mkdir()
        with pytest.raises(RuntimeError, match=r"No \.dist-info"):
            read_wheel_info(tmp_path, "ghost", "1.0")


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------


class TestCurrentRezPlatform:
    @pytest.mark.parametrize(
        ("platform", "machine", "expected"),
        [
            ("darwin", "arm64", ("osx", "arm64")),
            ("darwin", "x86_64", ("osx", "x86_64")),
            ("linux", "x86_64", ("linux", "x86_64")),
            ("linux", "amd64", ("linux", "x86_64")),
            ("linux", "aarch64", ("linux", "aarch64")),
            ("freebsd14", "amd64", ("linux", "x86_64")),
        ],
    )
    def test_posix(self, platform: str, machine: str, expected: tuple[str, str]) -> None:
        with (
            patch.object(rez_uv.sys, "platform", platform),
            patch.object(rez_uv.os, "uname", create=True, return_value=SimpleNamespace(machine=machine)),
        ):
            assert rez_uv._current_rez_platform() == expected

    def test_windows_uses_processor_architecture(self) -> None:
        with (
            patch.object(rez_uv.sys, "platform", "win32"),
            patch.object(rez_uv.os, "uname", create=True, return_value=SimpleNamespace(machine="AMD64")),
            patch.dict(os.environ, {"PROCESSOR_ARCHITECTURE": "ARM64"}),
        ):
            assert rez_uv._current_rez_platform() == ("windows", "ARM64")


# ---------------------------------------------------------------------------
# Requires conversion
# ---------------------------------------------------------------------------


class TestMarkerApplies:
    def test_true_marker(self) -> None:
        assert _marker_applies('python_version >= "3.0"') is True

    def test_false_marker(self) -> None:
        assert _marker_applies('python_version < "3.0"') is False

    def test_invalid_marker(self) -> None:
        assert _marker_applies("this is not a marker !!") is False


class TestParseRequiresDist:
    @pytest.mark.parametrize(
        ("req", "expected"),
        [
            ("requests", ("requests", "")),
            ("requests>=2.0,<3", ("requests", ">=2.0,<3")),
            ("requests[security,socks]>=2.0", ("requests", ">=2.0")),
            ("foo (>=1.0,<2)", ("foo", ">=1.0,<2")),
            ('baz>=1; python_version >= "3.0"', ("baz", ">=1")),
            ('qux; python_version < "3.0"', None),
            ("broken; not a marker !!", None),
            ("!!!", None),
        ],
    )
    def test_parse(self, req: str, expected: tuple[str, str] | None) -> None:
        assert _parse_requires_dist(req) == expected


class TestPep440SpecToRez:
    @pytest.mark.parametrize(
        ("specifier", "expected"),
        [
            ("", "foo"),
            ("   ", "foo"),
            (">=1.0,<2", "foo-1.0+<2"),
            (">=1.0", "foo-1.0+"),
            (">1.0", "foo-1.0+"),
            ("<2", "foo<2"),
            ("<=2", "foo<2"),
            ("==1.2.3", "foo==1.2.3"),
            ("==2.0+cu128", "foo==2.0"),
            ("~=1.4", "foo-1.4+<2"),
            ("~=1.4.2", "foo-1.4.2+<1.5"),
            ("~=1.0rc1.2", "foo-1.0rc1.2+<1.1"),
            ("~=1rc1", "foo-1rc1+<2"),
            ("!=1.5", "foo"),
            (">=1.0,!=1.5", "foo-1.0+"),
            ("garbage", "foo"),
            pytest.param(
                "==1.2.*",
                "foo-1.2",
                marks=pytest.mark.xfail(
                    strict=True,
                    reason="==X.Y.* is converted to an open lower bound (foo-1.2+), not the X.Y series",
                ),
            ),
        ],
    )
    def test_mapping(self, specifier: str, expected: str) -> None:
        assert _pep440_spec_to_rez("foo", specifier) == expected

    def test_family_name_is_normalised(self) -> None:
        assert _pep440_spec_to_rez("Ruamel.YAML", ">=0.17") == "ruamel_yaml-0.17+"


class TestBuildRequires:
    def test_sorted_and_marker_filtered(self) -> None:
        info = _wheel_info(
            requires_dist=[
                "urllib3<3,>=1.21.1",
                "idna>=2.5",
                'pywin32>=300; python_version < "3.0"',
                "charset-normalizer (>=2,<4)",
            ]
        )
        assert _build_requires(info) == ["charset_normalizer-2+<4", "idna-2.5+", "urllib3-1.21.1+<3"]

    def test_no_requires(self) -> None:
        assert _build_requires(_wheel_info()) == []


# ---------------------------------------------------------------------------
# package.py generation
# ---------------------------------------------------------------------------


class TestVariants:
    def test_platform_variant_subpath(self, linux_platform: None) -> None:  # noqa: ARG002
        assert _variant_subpath(has_platform=True, python_version="3.12") == (
            Path("platform-linux") / "arch-x86_64" / "python-3.12"
        )

    def test_pure_variant_subpath(self, linux_platform: None) -> None:  # noqa: ARG002
        assert _variant_subpath(has_platform=False, python_version="3.12") == Path("python-3.12")

    def test_read_existing_variants_missing_file(self, tmp_path: Path) -> None:
        assert _read_existing_variants(tmp_path / "package.py") == []

    def test_read_existing_variants(self, tmp_path: Path) -> None:
        pkg_file = tmp_path / "package.py"
        pkg_file.write_text("name = 'x'\nvariants = [['python-3.12'], ['python-3.13']]\n", encoding="utf-8")
        assert _read_existing_variants(pkg_file) == [["python-3.12"], ["python-3.13"]]

    def test_read_existing_variants_unparsable(self, tmp_path: Path, mock_logger: MagicMock) -> None:
        pkg_file = tmp_path / "package.py"
        pkg_file.write_text("variants = [[\n", encoding="utf-8")
        assert _read_existing_variants(pkg_file) == []
        mock_logger.debug.assert_called()

    def test_merge_variant_adds_new(self) -> None:
        assert _merge_variant([["python-3.12"]], ["python-3.13"]) == [["python-3.12"], ["python-3.13"]]

    def test_merge_variant_keeps_existing(self) -> None:
        existing = [["python-3.12"]]
        assert _merge_variant(existing, ["python-3.12"]) is existing


class TestWritePackagePy:
    def test_pure_package_content(self, tmp_path: Path, linux_platform: None) -> None:  # noqa: ARG002
        version_dir = tmp_path / "local" / "demo_pkg" / "1.0.0"
        info = _wheel_info(description="It's a demo", authors=["O'Brien", "Second"])

        _write_package_py(
            version_dir,
            "demo_pkg",
            info,
            ["idna-2.5+", "urllib3<3"],
            python_version="3.12",
            has_platform_variant=False,
        )

        content = (version_dir / "package.py").read_text(encoding="utf-8")
        assert "name = 'demo_pkg'" in content
        assert "version = '1.0.0'" in content
        assert "description = 'It\\'s a demo'" in content
        assert "authors = ['O\\'Brien', 'Second']" in content
        assert "    'idna-2.5+',\n    'urllib3<3',\n" in content
        assert "variants = [['python-3.12']]" in content
        assert "tools" not in content
        assert "env.PATH" not in content
        assert "pip_name = 'Demo-Pkg (1.0.0)'" in content
        assert "is_pure_python = True" in content
        assert "format_version = 2" in content
        compile(content, "package.py", "exec")

    def test_console_scripts_add_tools_and_path(self, tmp_path: Path, linux_platform: None) -> None:  # noqa: ARG002
        info = _wheel_info(console_scripts=["demo", "demo-admin"], description="", authors=[], is_pure_python=False)

        _write_package_py(tmp_path, "demo_pkg", info, [], python_version="3.12", has_platform_variant=True)

        content = (tmp_path / "package.py").read_text(encoding="utf-8")
        assert "tools = ['demo', 'demo-admin']" in content
        assert "env.PATH.append('{root}/bin')" in content
        assert "env.PATH.append('{root}/Scripts')" in content
        assert "variants = [['platform-linux', 'arch-x86_64', 'python-3.12']]" in content
        assert "description" not in content
        assert "authors" not in content
        assert "requires" not in content
        assert "is_pure_python = False" in content

    def test_second_platform_merges_variants(self, tmp_path: Path, mock_logger: MagicMock) -> None:
        info = _wheel_info(is_pure_python=False)
        with patch.object(rez_uv, "_current_rez_platform", return_value=LINUX):
            _write_package_py(tmp_path, "demo_pkg", info, [], python_version="3.12", has_platform_variant=True)
        with patch.object(rez_uv, "_current_rez_platform", return_value=OSX):
            _write_package_py(tmp_path, "demo_pkg", info, [], python_version="3.12", has_platform_variant=True)

        assert _read_existing_variants(tmp_path / "package.py") == [
            ["platform-linux", "arch-x86_64", "python-3.12"],
            ["platform-osx", "arch-arm64", "python-3.12"],
        ]
        mock_logger.info.assert_called()


# ---------------------------------------------------------------------------
# Package store helpers
# ---------------------------------------------------------------------------


class TestIsInstalled:
    def test_installed(self, tmp_path: Path) -> None:
        pkg_dir = tmp_path / "local" / "ruamel_yaml" / "0.18.6"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "package.py").write_text("", encoding="utf-8")
        assert is_installed("ruamel.yaml", "0.18.6", tmp_path) is True

    def test_not_installed(self, tmp_path: Path) -> None:
        assert is_installed("ruamel.yaml", "0.18.6", tmp_path) is False


class TestCopyPayload:
    def test_copies_modules_and_scripts(self, tmp_path: Path) -> None:
        install_dir = tmp_path / "install"
        _write_fake_wheel(install_dir, "demo", "1.0", scripts=("demo",))
        (install_dir / "demo" / "__pycache__").mkdir()
        (install_dir / "demo" / "__pycache__" / "x.pyc").write_bytes(b"")
        (install_dir / "demo-1.0.data").mkdir()
        (install_dir / "top_level.py").write_text("x = 1\n", encoding="utf-8")
        scripts_dir = install_dir / "Scripts"
        scripts_dir.mkdir()
        (scripts_dir / "demo.exe").write_bytes(b"")
        (scripts_dir / "unrelated.exe").write_bytes(b"")
        payload = tmp_path / "payload"

        _copy_payload(install_dir, payload, ["demo"])

        python_dir = payload / "python"
        assert (python_dir / "demo" / "__init__.py").exists()
        assert not (python_dir / "demo" / "__pycache__").exists()
        assert (python_dir / "demo-1.0.dist-info" / "METADATA").exists()
        assert (python_dir / "top_level.py").exists()
        assert not (python_dir / "demo-1.0.data").exists()
        assert not (python_dir / "bin").exists()
        assert sorted(p.name for p in (payload / "bin").iterdir()) == ["demo", "demo.exe"]

    def test_replaces_existing_payload(self, tmp_path: Path) -> None:
        install_dir = tmp_path / "install"
        _write_fake_wheel(install_dir, "demo", "1.0")
        (install_dir / "single.py").write_text("new = True\n", encoding="utf-8")
        python_dir = tmp_path / "payload" / "python"
        (python_dir / "demo").mkdir(parents=True)
        (python_dir / "demo" / "stale.py").write_text("", encoding="utf-8")
        (python_dir / "single.py").write_text("old = True\n", encoding="utf-8")

        _copy_payload(install_dir, tmp_path / "payload", [])

        assert not (python_dir / "demo" / "stale.py").exists()
        assert (python_dir / "single.py").read_text(encoding="utf-8") == "new = True\n"
        assert not (tmp_path / "payload" / "bin").exists()


# ---------------------------------------------------------------------------
# Install orchestration
# ---------------------------------------------------------------------------


class TestInstallOne:
    def test_installs_platform_wheel(self, tmp_path: Path, linux_platform: None, mock_logger: MagicMock) -> None:  # noqa: ARG002
        fake = FakeUv(
            {"torch": {"requires": ("filelock", "sympy>=1.13"), "scripts": ("torchrun",), "tag": "cp312-cp312-linux"}}
        )
        with patch.object(rez_uv.subprocess, "run", side_effect=fake):
            ok = _install_one(
                ResolvedPackage(pip_name="torch", version="2.7.0+cu128"),
                uv="uv",
                packages_dir=tmp_path,
                extra_index_url="https://download.pytorch.org/whl/cu128",
                extra_flags=["--torch-backend=auto"],
                python_version="3.12",
            )

        assert ok is True
        cmd = fake.calls[0]
        assert cmd[cmd.index("--extra-index-url") + 1] == "https://download.pytorch.org/whl/cu128"
        assert "--torch-backend=auto" in cmd
        version_dir = tmp_path / "local" / "torch" / "2.7.0"
        content = (version_dir / "package.py").read_text(encoding="utf-8")
        assert "version = '2.7.0'" in content
        assert "'filelock',\n    'sympy-1.13+'," in content
        payload = version_dir / "platform-linux" / "arch-x86_64" / "python-3.12"
        assert (payload / "python" / "torch" / "__init__.py").exists()
        assert (payload / "bin" / "torchrun").exists()

    def test_download_failure(self, tmp_path: Path, mock_logger: MagicMock) -> None:
        fake = FakeUv(fail_install={"ghost"})
        with patch.object(rez_uv.subprocess, "run", side_effect=fake):
            ok = _install_one(
                ResolvedPackage(pip_name="ghost", version="1.0"),
                uv="uv",
                packages_dir=tmp_path,
                extra_index_url=None,
                extra_flags=None,
                python_version="3.12",
            )

        assert ok is False
        assert "--extra-index-url" not in fake.calls[0]
        mock_logger.warning.assert_called()
        assert not (tmp_path / "local").exists()

    def test_metadata_failure(self, tmp_path: Path, mock_logger: MagicMock) -> None:
        fake = FakeUv()  # install "succeeds" but writes no .dist-info
        with patch.object(rez_uv.subprocess, "run", side_effect=fake):
            ok = _install_one(
                ResolvedPackage(pip_name="empty", version="1.0"),
                uv="uv",
                packages_dir=tmp_path,
                extra_index_url=None,
                extra_flags=None,
                python_version="3.12",
            )

        assert ok is False
        mock_logger.warning.assert_called()

    def test_write_failure_logs_path_and_reason(
        self,
        tmp_path: Path,
        linux_platform: None,  # noqa: ARG002
        mock_logger: MagicMock,
    ) -> None:
        fake = FakeUv({"six": {}})
        error = PermissionError(13, "Permission denied", str(tmp_path / "local" / "six"))
        with (
            patch.object(rez_uv.subprocess, "run", side_effect=fake),
            patch.object(rez_uv, "_write_package_py", side_effect=error),
        ):
            ok = _install_one(
                ResolvedPackage(pip_name="six", version="1.16.0"),
                uv="uv",
                packages_dir=tmp_path,
                extra_index_url=None,
                extra_flags=None,
                python_version="3.12",
            )

        assert ok is False
        args = mock_logger.error.call_args.args
        assert args[1:] == ("six", "1.16.0", str(tmp_path / "local" / "six"), "Permission denied")


class TestInstall:
    def test_installs_skips_and_reports_failures(
        self,
        tmp_path: Path,
        linux_platform: None,  # noqa: ARG002
        mock_logger: MagicMock,
    ) -> None:
        compile_output = "certifi==2024.2.2\n    # via requests\nidna==3.7\n    # via requests\nrequests==2.32.3\n"
        preinstalled = tmp_path / "local" / "certifi" / "2024.2.2"
        preinstalled.mkdir(parents=True)
        (preinstalled / "package.py").write_text("", encoding="utf-8")
        fake = FakeUv(
            {"requests": {"requires": ("certifi>=2017.4.17", "idna>=2.5,<4")}},
            compile_output=compile_output,
            fail_install={"idna"},
        )

        with patch.object(rez_uv.subprocess, "run", side_effect=fake):
            install("requests", packages_dir=tmp_path, python_version="3.12", uv_cmd="uv")

        installed_specs = [c[c.index("--target") + 2] for c in fake.calls if "install" in c]
        assert installed_specs == ["idna==3.7", "requests==2.32.3"]
        assert (tmp_path / "local" / "requests" / "2.32.3" / "package.py").exists()
        assert not (tmp_path / "local" / "idna").exists()
        summary = mock_logger.info.call_args_list[-1].args
        assert summary[1:] == (1, 1, ", 1 failed")
        mock_logger.warning.assert_called_with("[Rez][uv] failed packages: %s", "idna==3.7")

    def test_reinstall_ignores_existing(self, tmp_path: Path, linux_platform: None, mock_logger: MagicMock) -> None:  # noqa: ARG002
        preinstalled = tmp_path / "local" / "six" / "1.16.0"
        preinstalled.mkdir(parents=True)
        (preinstalled / "package.py").write_text("", encoding="utf-8")
        fake = FakeUv({"six": {}}, compile_output="six==1.16.0\n")

        with (
            patch.object(rez_uv.subprocess, "run", side_effect=fake),
            patch.object(rez_uv, "find_uv_bin", return_value="found-uv"),
        ):
            install(["six"], packages_dir=tmp_path, skip_installed=False)

        assert all(c[0] == "found-uv" for c in fake.calls)
        assert "name = 'six'" in (preinstalled / "package.py").read_text(encoding="utf-8")
        summary = mock_logger.info.call_args_list[-1].args
        assert summary[1:] == (1, 0, "")
        mock_logger.warning.assert_not_called()
