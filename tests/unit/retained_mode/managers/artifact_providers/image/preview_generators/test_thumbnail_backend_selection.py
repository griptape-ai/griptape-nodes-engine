"""Standard thumbnail selection works without the optional OpenImageIO package."""

import logging
import os
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete
from griptape_nodes.retained_mode.events.artifact_events import RegisterArtifactProviderResultSuccess
from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.thumbnail_generator import (
    THUMBNAIL_BACKEND,
    ThumbnailGenerator,
)


@pytest.mark.parametrize("backend", ["Pillow", "OpenImageIO"])
def test_engine_import_and_preview_with_selected_backend(tmp_path: Path, backend: str) -> None:
    """Fresh interpreters verify selection and ordinary publication for both backends."""
    if backend == "OpenImageIO" and find_spec("OpenImageIO") is None:
        pytest.skip("OpenImageIO selection requires the optional dependency")
    script = """
import importlib.abc
import logging
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
from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators import ThumbnailGenerator
from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.pil_thumbnail_generator import PILThumbnailGenerator
if sys.argv[2] == "Pillow":
    assert ThumbnailGenerator is PILThumbnailGenerator
else:
    from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.openimageio_thumbnail_generator import OpenImageIOThumbnailGenerator
    assert ThumbnailGenerator is OpenImageIOThumbnailGenerator
selected = ThumbnailGenerator
engine = Engine()
root = Path(sys.argv[1])
source = root / "source.png"
with Image.new("RGB", (8, 4), "red") as image:
    image.save(source)
generator = ThumbnailGenerator(str(source), "png", str(root), "preview.png", {}, engine=engine)
import asyncio
filename = asyncio.run(generator.attempt_generate_preview())
content = (root / filename).read_bytes()
assert content.startswith(b"\\x89PNG")
corrupt = root / "corrupt.png"
corrupt.write_bytes(b"\\x89PNG\\r\\n\\x1a\\ncorrupt")
bad = ThumbnailGenerator(str(corrupt), "png", str(root), "bad.png", {}, engine=engine)
try:
    asyncio.run(bad.attempt_generate_preview())
except (OSError, ValueError):
    pass
else:
    raise AssertionError("Corrupt source unexpectedly succeeded")
assert ThumbnailGenerator is selected
print("Selected backend preview succeeded")
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parents[7] / "src")
    result = subprocess.run(  # noqa: S603 - Fixed local interpreter and test script.
        [sys.executable, "-c", script, str(tmp_path), backend], capture_output=True, text=True, env=env, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Selected backend preview succeeded" in result.stdout


def test_broken_openimageio_import_is_not_hidden_by_fallback(tmp_path: Path) -> None:
    """An installed backend loader failure surfaces instead of silently selecting Pillow."""
    package = tmp_path / "OpenImageIO.py"
    package.write_text("raise ImportError('native library failed to load')\n", encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(tmp_path), str(Path(__file__).parents[7] / "src")])
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators import ThumbnailGenerator",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode != 0
    assert "native library failed to load" in result.stderr


@pytest.mark.asyncio
async def test_startup_logs_selected_backend_once_per_engine(
    engine: Engine, caplog: pytest.LogCaptureFixture, mocker: MockerFixture
) -> None:
    """Repeated initialization keeps one selected class and one informational announcement."""
    manager = engine.artifact_manager
    manager._thumbnail_backend_logged = False
    mocker.patch.object(
        manager,
        "on_handle_register_artifact_provider_request",
        return_value=RegisterArtifactProviderResultSuccess(result_details="Registered"),
    )
    selected = ThumbnailGenerator
    with caplog.at_level(logging.INFO, logger="griptape_nodes"):
        await manager.on_app_initialization_complete(AppInitializationComplete())
        await manager.on_app_initialization_complete(AppInitializationComplete())
    messages = [record.message for record in caplog.records if record.message.startswith("Standard thumbnail backend:")]
    assert messages == [f"Standard thumbnail backend: {THUMBNAIL_BACKEND}"]
    assert ThumbnailGenerator is selected
