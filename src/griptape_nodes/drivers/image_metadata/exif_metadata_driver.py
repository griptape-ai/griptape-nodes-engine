"""EXIF metadata injection and extraction driver for JPEG, TIFF, MPO, and WEBP formats."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any

from griptape_nodes.drivers.image_metadata.base_image_metadata_driver import (
    BaseImageMetadataDriver,
    MetadataImageInput,
    opening_metadata_image,
)
from griptape_nodes.image import EXIF_GPSINFO_TAG, Exif, ImageFormat
from griptape_nodes.image.exif_tags import GPS_TAGS, TAGS

logger = logging.getLogger("griptape_nodes")

EXIF_USERCOMMENT_TAG = 0x9286
EXIF_MAKERNOTE_TAG = 0x927C
GPS_COORD_TUPLE_LENGTH = 3


class ExifMetadataDriver(BaseImageMetadataDriver):
    """Bidirectional driver for EXIF metadata.

    Supports reading standard EXIF tags, GPS coordinates, and JSON-encoded ``UserComment``,
    and writing custom key-value metadata into ``UserComment`` while preserving existing EXIF tags
    and ICC color profiles.
    """

    def get_supported_formats(self) -> list[str]:
        """Return list of format strings this driver supports."""
        return ["JPEG", "TIFF", "MPO", "WEBP"]

    def inject_metadata(self, image: MetadataImageInput, metadata: dict[str, str]) -> bytes:
        """Inject metadata into EXIF UserComment field as JSON."""
        with opening_metadata_image(image) as gt_image:
            if gt_image.format is None or gt_image.format not in self.get_supported_formats():
                msg = (
                    f"Attempted to add EXIF metadata to an image. "
                    f"Failed because {gt_image.format or 'an unknown format'} does not hold EXIF."
                )
                raise ValueError(msg)

            exif_data = gt_image.metadata.exif or Exif()
            exif_data[EXIF_USERCOMMENT_TAG] = json.dumps(metadata, separators=(",", ":"))
            target_format = ImageFormat.JPEG if gt_image.format == ImageFormat.MPO else gt_image.format
            return gt_image.encode(target_format, metadata=replace(gt_image.metadata, exif=exif_data))

    def extract_metadata(self, image: MetadataImageInput) -> dict[str, str]:
        """Extract standard EXIF tags, GPS data, and custom UserComment entries."""
        with opening_metadata_image(image) as gt_image:
            exif_data = gt_image.metadata.exif
            if not exif_data:
                return {}

            metadata: dict[str, str] = {}
            self._extract_standard_tags(exif_data, metadata)
            self._extract_gps_metadata(exif_data, metadata)
            self._extract_user_comment_metadata(exif_data, metadata)
            return metadata

    def _exif_value_to_string(self, value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="ignore").strip("\x00")
        if isinstance(value, (tuple, list)):
            return ", ".join(str(v) for v in value)
        return str(value)

    def _format_gps_coordinate(self, coord_tuple: tuple) -> str:
        if not coord_tuple or len(coord_tuple) != GPS_COORD_TUPLE_LENGTH:
            return str(coord_tuple)

        try:
            degrees = float(coord_tuple[0])
            minutes = float(coord_tuple[1])
            seconds = float(coord_tuple[2])
            decimal_degrees = degrees + (minutes / 60.0) + (seconds / 3600.0)
        except (TypeError, ValueError, ZeroDivisionError):
            return self._exif_value_to_string(coord_tuple)
        return f"{decimal_degrees:.6f}"

    def _extract_standard_tags(self, exif_data: Exif, metadata: dict[str, str]) -> None:
        for tag_id, value in exif_data.items():
            if tag_id in (EXIF_USERCOMMENT_TAG, EXIF_GPSINFO_TAG, EXIF_MAKERNOTE_TAG):
                continue
            tag_name = TAGS.get(tag_id, f"Tag_{tag_id}")
            metadata[tag_name] = self._exif_value_to_string(value)

    def _extract_gps_metadata(self, exif_data: Exif, metadata: dict[str, str]) -> None:
        gps_ifd = exif_data.gps
        if not gps_ifd:
            return

        for gps_tag_id, gps_value in gps_ifd.items():
            gps_tag_name = GPS_TAGS.get(gps_tag_id, f"GPSTag_{gps_tag_id}")
            if gps_tag_id in (2, 4):
                metadata[f"GPS_{gps_tag_name}"] = self._format_gps_coordinate(gps_value)
            elif gps_tag_id in (1, 3):
                metadata[f"GPS_{gps_tag_name}"] = str(gps_value)
            else:
                metadata[f"GPS_{gps_tag_name}"] = self._exif_value_to_string(gps_value)

    def _extract_user_comment_metadata(self, exif_data: Exif, metadata: dict[str, str]) -> None:
        user_comment = exif_data.get(EXIF_USERCOMMENT_TAG)
        if not user_comment:
            return

        if isinstance(user_comment, bytes):
            comment_str = user_comment.decode("utf-8", errors="ignore").strip("\x00")
        else:
            comment_str = str(user_comment)

        try:
            custom_metadata = json.loads(comment_str)
        except (json.JSONDecodeError, ValueError) as e:
            logger.debug("Could not parse UserComment as JSON: %s", e)
            return

        if not isinstance(custom_metadata, dict):
            logger.debug("UserComment is not a dict, skipping custom metadata")
            return

        for key, value in custom_metadata.items():
            metadata[key] = str(value)

        if custom_metadata:
            logger.debug("Merged %d custom metadata entries from EXIF UserComment", len(custom_metadata))
