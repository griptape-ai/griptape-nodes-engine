"""Test that the built-in image provider selects the standard thumbnail backend."""

import os
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path

import pytest

from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.artifact_events import (
    RegisterArtifactProviderRequest,
    RegisterArtifactProviderResultSuccess,
)
from griptape_nodes.retained_mode.managers.artifact_providers.image.image_artifact_provider import ImageArtifactProvider
from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.pil_thumbnail_generator import (
    PILThumbnailGenerator,
)


@pytest.mark.parametrize("backend", ["Pillow", "OpenImageIO"])
def test_startup_registers_image_provider_and_generates_preview(tmp_path: Path, backend: str) -> None:
    """Exercise provider registration and preview publication in a fresh interpreter."""
    if backend == "OpenImageIO" and find_spec("OpenImageIO") is None:
        pytest.skip("OpenImageIO selection requires the optional dependency")
    script = """
import asyncio
import importlib.abc
import sys
from pathlib import Path
from PIL import Image

class BlockOpenImageIO(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "OpenImageIO" or fullname.startswith("OpenImageIO."):
            raise ModuleNotFoundError("OpenImageIO intentionally absent", name="OpenImageIO")
        return None

if sys.argv[2] == "Pillow":
    sys.meta_path.insert(0, BlockOpenImageIO())

from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete
from griptape_nodes.retained_mode.events.artifact_events import ListArtifactProvidersRequest, ListArtifactProvidersResultSuccess

async def main():
    engine = Engine()
    await engine.artifact_manager.on_app_initialization_complete(AppInitializationComplete())
    result = await engine.ahandle_request(ListArtifactProvidersRequest())
    assert isinstance(result, ListArtifactProvidersResultSuccess), result
    assert result.friendly_names.count("Image") == 1

    provider = engine.artifact_manager.provider_for_write("source.png")
    assert provider is not None
    if sys.argv[2] == "Pillow":
        from griptape_nodes.retained_mode.managers.artifact_providers.image.image_artifact_provider import ImageArtifactProvider
        from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.pil_thumbnail_generator import PILThumbnailGenerator
        assert type(provider) is ImageArtifactProvider
        assert PILThumbnailGenerator in provider.get_default_preview_generators()
    else:
        from griptape_nodes.retained_mode.managers.artifact_providers.image.openimageio_artifact_provider import OpenImageIOArtifactProvider
        from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.openimageio_thumbnail_generator import OpenImageIOThumbnailGenerator
        assert type(provider) is OpenImageIOArtifactProvider
        assert OpenImageIOThumbnailGenerator in provider.get_default_preview_generators()

    root = Path(sys.argv[1])
    source = root / "source.png"
    with Image.new("RGB", (8, 4), "red") as image:
        image.save(source)
    generator_name = provider.get_default_preview_generator()
    filename = await provider.attempt_generate_preview(generator_name, str(source), "png", str(root), "preview.png", {})
    content = (root / filename).read_bytes()
    assert content.startswith(b"\\x89PNG")

    corrupt = root / "corrupt.png"
    corrupt.write_bytes(b"\\x89PNG\\r\\n\\x1a\\ncorrupt")
    try:
        await provider.attempt_generate_preview(generator_name, str(corrupt), "png", str(root), "bad.png", {})
    except (OSError, ValueError):
        pass
    else:
        raise AssertionError("Corrupt source unexpectedly succeeded")

asyncio.run(main())
"""
    env = os.environ.copy()
    env["GTN_CONFIG_WORKSPACE_DIRECTORY"] = str(tmp_path)
    env["PYTHONPATH"] = str(Path(__file__).parents[7] / "src")
    result = subprocess.run(  # noqa: S603 - Fixed local interpreter and test script.
        [sys.executable, "-c", script, str(tmp_path), backend], capture_output=True, text=True, env=env, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_broken_openimageio_import_is_not_hidden_by_fallback(tmp_path: Path) -> None:
    """An installed backend loader failure surfaces at registration."""
    package = tmp_path / "OpenImageIO.py"
    package.write_text("raise ImportError('native library failed to load')\n", encoding="utf-8")
    env = os.environ.copy()
    env["GTN_CONFIG_WORKSPACE_DIRECTORY"] = str(tmp_path)
    env["PYTHONPATH"] = os.pathsep.join([str(tmp_path), str(Path(__file__).parents[7] / "src")])
    script = """
import asyncio
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete

engine = Engine()
asyncio.run(engine.artifact_manager.on_app_initialization_complete(AppInitializationComplete()))
"""
    result = subprocess.run(  # noqa: S603 - Fixed local interpreter and test script.
        [sys.executable, "-c", script], capture_output=True, text=True, env=env, check=False
    )
    assert result.returncode != 0
    assert "native library failed to load" in result.stderr


def test_explicit_pillow_registration_uses_pillow_with_openimageio_installed(engine: Engine) -> None:
    """Explicit Pillow registration keeps its standard generator when OpenImageIO is available."""
    if find_spec("OpenImageIO") is None:
        pytest.skip("This case requires OpenImageIO alongside Pillow")

    result = engine.handle_request(RegisterArtifactProviderRequest(provider_class=ImageArtifactProvider))
    assert isinstance(result, RegisterArtifactProviderResultSuccess)
    provider = engine.artifact_manager.provider_for_write("test.png")
    assert type(provider) is ImageArtifactProvider
    standard = [
        generator
        for generator in provider.get_default_preview_generators()
        if generator.get_friendly_name() == provider.get_default_preview_generator()
    ]
    assert standard == [PILThumbnailGenerator]
