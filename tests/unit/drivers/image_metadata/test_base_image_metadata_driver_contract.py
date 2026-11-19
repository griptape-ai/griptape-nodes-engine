"""The metadata driver's abstract implementations fail explicitly if called."""

from unittest.mock import MagicMock

import pytest

from griptape_nodes.drivers.image_metadata.base_image_metadata_driver import BaseImageMetadataDriver
from griptape_nodes.image.cache import invalidate_image_cache
from griptape_nodes.image.oiio_config import invalidate_image_cache as exported_invalidator


class TestBaseImageMetadataDriverContract:
    def test_supported_formats_requires_implementation(self) -> None:
        with pytest.raises(NotImplementedError):
            BaseImageMetadataDriver.get_supported_formats(MagicMock())  # pyright: ignore[reportAbstractUsage]

    def test_injection_requires_implementation(self) -> None:
        with pytest.raises(NotImplementedError):
            BaseImageMetadataDriver.inject_metadata(MagicMock(), b"image", {})  # pyright: ignore[reportAbstractUsage]

    def test_extraction_requires_implementation(self) -> None:
        with pytest.raises(NotImplementedError):
            BaseImageMetadataDriver.extract_metadata(MagicMock(), b"image")  # pyright: ignore[reportAbstractUsage]

    def test_configuration_module_reexports_cache_invalidator(self) -> None:
        assert exported_invalidator is invalidate_image_cache
