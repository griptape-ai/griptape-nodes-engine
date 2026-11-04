"""Registry for image metadata injection drivers."""

from typing import ClassVar

from griptape_nodes.drivers.image_metadata.base_image_metadata_driver import BaseImageMetadataDriver


class ImageMetadataDriverRegistry:
    """Registry for image metadata injection drivers.

    Provides centralized registration and lookup of metadata injection drivers
    based on image format.
    """

    _drivers: ClassVar[list[BaseImageMetadataDriver]] = []

    @classmethod
    def register_driver(cls, driver: BaseImageMetadataDriver) -> None:
        """Register a metadata injection driver.

        Args:
            driver: Driver instance to register
        """
        cls._drivers.append(driver)

    @classmethod
    def get_driver_for_format(cls, format_str: str) -> BaseImageMetadataDriver | None:
        """Get the first driver that supports the given format.

        Args:
            format_str: PIL format string (e.g., "PNG", "JPEG")

        Returns:
            Driver instance or None if no driver supports format
        """
        for driver in cls._drivers:
            if format_str in driver.get_supported_formats():
                return driver
        return None

    @classmethod
    def get_supported_formats(cls) -> set[str]:
        """Get all formats supported by registered drivers.

        Returns:
            Set of format strings supported by any registered driver
        """
        formats = set()
        for driver in cls._drivers:
            formats.update(driver.get_supported_formats())
        return formats
