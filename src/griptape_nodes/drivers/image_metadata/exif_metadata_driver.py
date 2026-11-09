"""EXIF metadata injection and extraction driver for JPEG, TIFF, and MPO formats."""

import json
import logging
from io import BytesIO
from typing import Any

from PIL import Image
from PIL.ExifTags import GPSTAGS, TAGS

from griptape_nodes.drivers.image_metadata.base_image_metadata_driver import BaseImageMetadataDriver

logger = logging.getLogger("griptape_nodes")

# EXIF tag IDs
EXIF_USERCOMMENT_TAG = 0x9286
EXIF_GPSINFO_TAG = 0x8825  # 34853

# GPS coordinate tuple length
GPS_COORD_TUPLE_LENGTH = 3


class ExifMetadataDriver(BaseImageMetadataDriver):
    """Bidirectional driver for EXIF metadata.

    Supports reading ALL EXIF metadata (standard tags, GPS data, custom UserComment)
    and writing custom metadata to EXIF UserComment field as JSON.
    Preserves all existing EXIF data when writing.
    All EXIF-specific logic is encapsulated in this driver.
    """

    def get_supported_formats(self) -> list[str]:
        """Return list of PIL format strings this driver supports.

        Returns:
            List containing "JPEG", "TIFF", "MPO"
        """
        return ["JPEG", "TIFF", "MPO", "WEBP"]

    def inject_metadata(self, data: bytes, pil_image: Image.Image, metadata: dict[str, str]) -> bytes:  # noqa: ARG002
        """Inject metadata into EXIF UserComment field as JSON.

        Serializes metadata dictionary to JSON and stores in EXIF UserComment.
        Preserves all existing EXIF tags.

        Args:
            data: Encoded image bytes that pil_image was opened from (unused; the image is re-saved)
            pil_image: PIL Image to inject metadata into
            metadata: Dictionary of key-value pairs to inject

        Returns:
            Image bytes with metadata injected

        Raises:
            Exception: On EXIF save errors
        """
        # Get existing EXIF data to preserve it
        exif_data = pil_image.getexif()

        # Serialize metadata to JSON for UserComment field
        metadata_json = json.dumps(metadata, separators=(",", ":"))

        # Set UserComment field
        exif_data[EXIF_USERCOMMENT_TAG] = metadata_json

        # Save with updated EXIF
        output_buffer = BytesIO()
        pil_image.save(output_buffer, format=pil_image.format, exif=exif_data)
        return output_buffer.getvalue()

    def extract_metadata(self, pil_image: Image.Image) -> dict[str, str]:
        """Extract ALL EXIF metadata including standard tags, GPS data, and custom UserComment.

        Returns combined metadata from all sources:
        - Standard EXIF tags (Make, Model, DateTime, etc.)
        - GPS metadata (prefixed with 'GPS_')
        - Custom metadata from UserComment field (JSON parsed)

        Args:
            pil_image: PIL Image to extract metadata from

        Returns:
            Dictionary of all metadata key-value pairs, empty dict if no EXIF data
        """
        exif_data = pil_image.getexif()
        if not exif_data:
            return {}

        metadata = {}

        # Extract standard EXIF tags
        self._extract_standard_tags(exif_data, metadata)

        # Extract GPS metadata
        self._extract_gps_metadata(exif_data, metadata)

        # Extract custom UserComment metadata
        self._extract_user_comment_metadata(exif_data, metadata)

        return metadata

    def _exif_value_to_string(self, value: Any) -> str:
        """Convert EXIF value to readable string format.

        Args:
            value: EXIF value (can be bytes, tuple, list, int, etc.)

        Returns:
            String representation of the value
        """
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8", errors="ignore").strip("\x00")
            except Exception:
                return str(value)
        elif isinstance(value, (tuple, list)):
            return ", ".join(str(v) for v in value)
        return str(value)

    def _format_gps_coordinate(self, coord_tuple: tuple) -> str:
        """Format GPS coordinate tuple to decimal degrees string.

        Args:
            coord_tuple: Tuple of (degrees, minutes, seconds) as rational numbers

        Returns:
            Decimal degrees as string
        """
        if not coord_tuple or len(coord_tuple) != GPS_COORD_TUPLE_LENGTH:
            return str(coord_tuple)

        try:
            degrees = float(coord_tuple[0])
            minutes = float(coord_tuple[1])
            seconds = float(coord_tuple[2])

            decimal_degrees = degrees + (minutes / 60.0) + (seconds / 3600.0)
        except Exception:
            return self._exif_value_to_string(coord_tuple)
        else:
            return f"{decimal_degrees:.6f}"

    def _extract_standard_tags(self, exif_data: Any, metadata: dict[str, str]) -> None:
        """Extract standard EXIF tags.

        Args:
            exif_data: EXIF data object from PIL
            metadata: Dictionary to populate with standard tags (modified in-place)
        """
        for tag_id, value in exif_data.items():
            # Get tag name
            tag_name = TAGS.get(tag_id, f"Tag_{tag_id}")

            # Skip UserComment and GPSInfo, we'll handle them separately
            if tag_id in (EXIF_USERCOMMENT_TAG, EXIF_GPSINFO_TAG):
                continue

            # Convert value to string
            metadata[tag_name] = self._exif_value_to_string(value)

    def _extract_gps_metadata(self, exif_data: Any, metadata: dict[str, str]) -> None:
        """Extract GPS metadata from EXIF data.

        Formats GPS coordinates as decimal degrees and prefixes all GPS tags with 'GPS_'.

        Args:
            exif_data: EXIF data object from PIL
            metadata: Dictionary to populate with GPS metadata (modified in-place)
        """
        gps_ifd = exif_data.get_ifd(EXIF_GPSINFO_TAG)
        if not gps_ifd:
            return

        for gps_tag_id, gps_value in gps_ifd.items():
            gps_tag_name = GPSTAGS.get(gps_tag_id, f"GPSTag_{gps_tag_id}")

            # Format GPS coordinate values specially
            if gps_tag_id in (1, 2, 3, 4):  # GPSLatitudeRef, GPSLatitude, GPSLongitudeRef, GPSLongitude
                if gps_tag_id in (2, 4):  # Latitude or Longitude tuple
                    metadata[f"GPS_{gps_tag_name}"] = self._format_gps_coordinate(gps_value)
                else:
                    metadata[f"GPS_{gps_tag_name}"] = str(gps_value)
            else:
                metadata[f"GPS_{gps_tag_name}"] = self._exif_value_to_string(gps_value)

    def _extract_user_comment_metadata(self, exif_data: Any, metadata: dict[str, str]) -> None:
        """Extract custom metadata from EXIF UserComment field.

        Args:
            exif_data: EXIF data object from PIL
            metadata: Dictionary to populate with custom metadata (modified in-place)
        """
        user_comment = exif_data.get(EXIF_USERCOMMENT_TAG)
        if not user_comment:
            return

        # Parse JSON from UserComment field
        try:
            # Pillow handles the character code prefix, but might return bytes or string
            if isinstance(user_comment, bytes):
                comment_str = user_comment.decode("utf-8", errors="ignore").strip("\x00")
            else:
                comment_str = str(user_comment)

            custom_metadata = json.loads(comment_str)
            if not isinstance(custom_metadata, dict):
                logger.debug("UserComment is not a dict, skipping custom metadata")
                return

            # Merge custom metadata directly into metadata dict
            for key, value in custom_metadata.items():
                metadata[key] = str(value)

            if custom_metadata:
                logger.debug("Merged %d custom metadata entries from EXIF UserComment", len(custom_metadata))
        except (json.JSONDecodeError, ValueError) as e:
            logger.debug("Could not parse UserComment as JSON: %s", e)
        except Exception as e:
            logger.debug("Unexpected error parsing UserComment: %s", e)
