"""Unit tests for version_utils module."""

from __future__ import annotations

import importlib.metadata
from unittest.mock import patch

import pytest

from griptape_nodes.utils import version_utils
from griptape_nodes.utils.version_utils import (
    ENGINE_PACKAGE_NAME,
    format_version_string,
    get_complete_version_string,
    get_install_source,
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


if __name__ == "__main__":
    pytest.main([__file__])
