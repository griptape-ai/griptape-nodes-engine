"""Verify engine image processing works when Pillow imports are unavailable."""

from __future__ import annotations

import importlib.metadata
import subprocess
import sys
from pathlib import Path


class TestPillowRemoved:
    def test_engine_does_not_declare_pillow(self) -> None:
        requirements = importlib.metadata.requires("griptape-nodes-engine") or []
        assert all(not requirement.lower().startswith("pillow") for requirement in requirements)

    def test_image_preview_with_pil_imports_blocked(self) -> None:
        source = """
import importlib.abc
import sys

class BlockPillow(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'PIL' or fullname.startswith('PIL.'):
            raise ImportError('Pillow intentionally unavailable')
        return None

sys.meta_path.insert(0, BlockPillow())
from griptape_nodes.image import GriptapeImage, GriptapeImagePipeline, ImageFormat
from tests.unit.image.sample_images import solid, encode
with GriptapeImage.open(encode(solid(64, 64), 'png')) as image:
    with GriptapeImagePipeline(image).thumbnail(32, 32).end() as preview:
        assert preview.encode(ImageFormat.WEBP)
assert not any(name == 'PIL' or name.startswith('PIL.') for name in sys.modules)
"""
        result = subprocess.run(  # noqa: S603 - fixed code and the current test interpreter
            [sys.executable, "-c", source],
            cwd=Path(__file__).resolve().parents[3],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
