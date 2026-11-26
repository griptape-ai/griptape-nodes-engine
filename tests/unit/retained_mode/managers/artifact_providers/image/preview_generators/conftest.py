"""Lazy worker-local procedural image fixtures."""

from collections.abc import Callable
from importlib.util import find_spec
from pathlib import Path

import pytest

if find_spec("OpenImageIO") is not None:
    from tests.unit.retained_mode.managers.artifact_providers.image.preview_generators.image_fixtures.image_generation import (
        write_variant,
    )
else:
    write_variant = None
from tests.unit.retained_mode.managers.artifact_providers.image.preview_generators.image_fixtures.image_specifications import (
    load_thumbnail_specifications,
)


@pytest.fixture(scope="session")
def image_source(tmp_path_factory: pytest.TempPathFactory) -> Callable[[str, str], Path]:
    """Generate selected immutable inputs once per worker, outside the repository."""
    writer = write_variant
    if writer is None:
        pytest.skip("Procedural image sources require OpenImageIO")
    specification = load_thumbnail_specifications()
    images = {image.id: image for image in specification.images}
    directory = tmp_path_factory.mktemp("declarative-sources")
    cached: dict[tuple[str, str], Path] = {}

    def get_source(image_id: str, variant_id: str) -> Path:
        key = (image_id, variant_id)
        if key not in cached:
            image = images[image_id]
            variant = next(variant for variant in image.variants if variant.id == variant_id)
            cached[key] = writer(image, variant, directory)
        return cached[key]

    return get_source
