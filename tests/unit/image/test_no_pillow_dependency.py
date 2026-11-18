"""Verify Pillow is absent from the engine dependency graph and environment."""

from __future__ import annotations

import importlib.metadata
import importlib.util

import pytest


class TestPillowRemoved:
    def test_pillow_distribution_is_not_installed(self) -> None:
        with pytest.raises(importlib.metadata.PackageNotFoundError):
            importlib.metadata.version("pillow")

    def test_pil_module_is_not_importable(self) -> None:
        assert importlib.util.find_spec("PIL") is None
