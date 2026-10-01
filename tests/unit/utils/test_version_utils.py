"""Unit tests for version_utils module."""

from __future__ import annotations

import importlib.metadata
import sys
import sysconfig
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from griptape_nodes.utils import version_utils
from griptape_nodes.utils.version_utils import (
    ENGINE_PACKAGE_NAME,
    ShadowedPackage,
    engine_package_floors,
    format_version_string,
    get_complete_version_string,
    get_install_source,
    packages_shadowing_the_engine,
)

_MODULE = "griptape_nodes.utils.version_utils"


class TestFormatVersionString:
    """Test format_version_string utility function."""

    def test_formats_pypi_source(self) -> None:
        with patch(f"{_MODULE}.get_install_source", return_value=("pypi", None)):
            assert format_version_string("v1.2.3", "griptape-nodes") == "v1.2.3 (pypi)"

    def test_formats_git_source_with_commit(self) -> None:
        with patch(f"{_MODULE}.get_install_source", return_value=("git", "abc1234")) as install_source_mock:
            assert format_version_string("v1.2.3", "griptape-nodes") == "v1.2.3 (git - abc1234)"
            install_source_mock.assert_called_once_with("griptape-nodes")

    def test_defaults_to_engine_package(self) -> None:
        with patch(f"{_MODULE}.get_install_source", return_value=("pypi", None)) as install_source_mock:
            format_version_string("v1.2.3")
            install_source_mock.assert_called_once_with(ENGINE_PACKAGE_NAME)


class TestGetCompleteVersionString:
    """Test get_complete_version_string targets the engine package."""

    def test_uses_engine_package(self) -> None:
        with (
            patch(f"{_MODULE}.get_current_version", return_value="v9.9.9"),
            patch(f"{_MODULE}.get_install_source", return_value=("pypi", None)) as install_source_mock,
        ):
            assert get_complete_version_string() == "v9.9.9 (pypi)"
            install_source_mock.assert_called_once_with(ENGINE_PACKAGE_NAME)


class TestGetInstallSource:
    """Test get_install_source respects the requested package name."""

    def test_defaults_to_engine_package(self) -> None:
        with (
            patch.object(version_utils.importlib.metadata, "distributions", return_value=[]),
            patch.object(
                version_utils.importlib.metadata,
                "distribution",
                side_effect=importlib.metadata.PackageNotFoundError,
            ) as distribution_mock,
        ):
            assert get_install_source() == ("unknown", None)
            distribution_mock.assert_called_once_with(ENGINE_PACKAGE_NAME)

    def test_accepts_explicit_package_name(self) -> None:
        with (
            patch.object(version_utils.importlib.metadata, "distributions", return_value=[]),
            patch.object(
                version_utils.importlib.metadata,
                "distribution",
                side_effect=importlib.metadata.PackageNotFoundError,
            ) as distribution_mock,
        ):
            get_install_source("griptape-nodes")
            distribution_mock.assert_called_once_with("griptape-nodes")


def _distribution(name: str | None, version: str, *, direct_url: str | None = None) -> MagicMock:
    distribution = MagicMock()
    distribution.metadata = {"Name": name} if name is not None else {}
    distribution.version = version
    distribution.read_text.return_value = direct_url
    return distribution


class TestEnginePackageFloors:
    """Test engine_package_floors renders a constraint for every package the engine has installed."""

    def test_every_package_gets_a_floor(self) -> None:
        with patch.object(
            version_utils.importlib.metadata,
            "distributions",
            return_value=[_distribution("griptape", "1.13.0"), _distribution("anyio", "4.14.2")],
        ):
            assert engine_package_floors() == ("anyio>=4.14.2", "griptape>=1.13.0")

    def test_names_are_canonicalized(self) -> None:
        """A dist-info reads `typing_extensions`, but only `typing-extensions` constrains a resolver."""
        with patch.object(
            version_utils.importlib.metadata,
            "distributions",
            return_value=[_distribution("typing_extensions", "4.15.0")],
        ):
            assert engine_package_floors() == ("typing-extensions>=4.15.0",)

    def test_the_engine_itself_gets_no_floor(self) -> None:
        """A floor on the engine would replace an editable engine install with a wheel."""
        with patch.object(
            version_utils.importlib.metadata,
            "distributions",
            return_value=[_distribution(ENGINE_PACKAGE_NAME, "0.103.0"), _distribution("griptape", "1.13.0")],
        ):
            assert engine_package_floors() == ("griptape>=1.13.0",)

    def test_a_package_installed_from_git_or_a_path_gets_no_floor(self) -> None:
        """Its version is not one an index has to carry, so a floor on it can be unsatisfiable."""
        with patch.object(
            version_utils.importlib.metadata,
            "distributions",
            return_value=[
                _distribution("griptape-cloud-client", "0.2.0", direct_url='{"url": "https://example.invalid"}'),
                _distribution("griptape", "1.13.0"),
            ],
        ):
            assert engine_package_floors() == ("griptape>=1.13.0",)

    def test_a_distribution_with_unreadable_metadata_is_skipped(self) -> None:
        with patch.object(
            version_utils.importlib.metadata,
            "distributions",
            return_value=[_distribution(None, "1.0.0"), _distribution("griptape", "1.13.0")],
        ):
            assert engine_package_floors() == ("griptape>=1.13.0",)


class TestEnginePackageFloorsReadTheEngineEnvironmentOnly:
    """A bare `distributions()` walks `sys.path`, where library environments sit ahead of the engine's.

    Floors read from there name the stale versions they exist to displace, so the install they
    constrain is free to keep them and the constraint file still looks right.
    """

    def test_the_floor_is_the_engine_version_not_the_one_spliced_ahead_of_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        library_site_packages = "/library/.venv/lib/python3.12/site-packages"

        def fake_distributions(*, path: list[str] | None = None) -> list[MagicMock]:
            if path is None or library_site_packages in path:
                return [_distribution("griptape", "1.10.2")]
            return [_distribution("griptape", "1.13.0")]

        monkeypatch.setattr(sys, "path", [library_site_packages, sysconfig.get_path("purelib")])
        with patch.object(version_utils.importlib.metadata, "distributions", fake_distributions):
            assert engine_package_floors() == ("griptape>=1.13.0",)


class TestPackagesShadowingTheEngine:
    """Test packages_shadowing_the_engine names what a library environment supplies too old."""

    def test_a_package_older_than_the_engine_is_reported(self) -> None:
        """Older by PEP 440, which orders 1.9.4 below 1.13.0 where a string compare does not."""
        with (
            patch(f"{_MODULE}.engine_package_versions", return_value={"griptape": "1.13.0"}),
            patch.object(
                version_utils.importlib.metadata, "distributions", return_value=[_distribution("griptape", "1.9.4")]
            ),
        ):
            assert packages_shadowing_the_engine([Path("/library/.venv")]) == (
                ShadowedPackage(name="griptape", library_version="1.9.4", engine_version="1.13.0"),
            )

    def test_a_package_at_or_above_the_engine_version_is_not_reported(self) -> None:
        """The engine imports its own version or newer, which is what the floors exist to allow."""
        with (
            patch(f"{_MODULE}.engine_package_versions", return_value={"griptape": "1.13.0", "anyio": "4.14.2"}),
            patch.object(
                version_utils.importlib.metadata,
                "distributions",
                return_value=[_distribution("griptape", "1.13.0"), _distribution("anyio", "4.15.1")],
            ),
        ):
            assert packages_shadowing_the_engine([Path("/library/.venv")]) == ()

    def test_a_package_the_engine_does_not_have_is_not_reported(self) -> None:
        """A library's own dependencies shadow nothing; only what the engine also imports matters."""
        with (
            patch(f"{_MODULE}.engine_package_versions", return_value={"griptape": "1.13.0"}),
            patch.object(
                version_utils.importlib.metadata, "distributions", return_value=[_distribution("torch", "2.7.0")]
            ),
        ):
            assert packages_shadowing_the_engine([Path("/library/.venv")]) == ()

    def test_the_oldest_copy_across_environments_is_the_one_named(self) -> None:
        """A library's two environments resolve separately, so they can disagree."""
        edit_venv = Path("/library/.venv")

        # Matched as the caller renders it, since a directory separator is not the same character
        # on every platform. Exact equality, because `.venv` is a prefix of `.venv-exec`.
        def fake_distributions(*, path: list[str]) -> list[MagicMock]:
            if path == [str(edit_venv)]:
                return [_distribution("griptape", "1.9.4")]
            return [_distribution("griptape", "1.12.0")]

        with (
            patch(f"{_MODULE}.engine_package_versions", return_value={"griptape": "1.13.0"}),
            patch.object(version_utils.importlib.metadata, "distributions", fake_distributions),
        ):
            shadowed = packages_shadowing_the_engine([Path("/library/.venv-exec"), edit_venv])

        assert shadowed == (ShadowedPackage(name="griptape", library_version="1.9.4", engine_version="1.13.0"),)

    def test_an_unparsable_version_is_skipped(self) -> None:
        with (
            patch(f"{_MODULE}.engine_package_versions", return_value={"griptape": "1.13.0"}),
            patch.object(
                version_utils.importlib.metadata,
                "distributions",
                return_value=[_distribution("griptape", "not-a-version")],
            ),
        ):
            assert packages_shadowing_the_engine([Path("/library/.venv")]) == ()


if __name__ == "__main__":
    pytest.main([__file__])
