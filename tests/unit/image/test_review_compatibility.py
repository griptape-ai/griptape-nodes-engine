"""Legacy driver and flow extraction failures from the image migration."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock, patch

import pytest

from griptape_nodes.drivers.image_metadata.base_image_metadata_driver import MetadataImageInput, opening_metadata_image
from griptape_nodes.image import Exif
from griptape_nodes.image.exif import EXIF_USER_COMMENT_TAG
from griptape_nodes.image.png_text_chunks import insert_text_chunks, png_chunk, should_compress_text
from griptape_nodes.retained_mode.events.flow_events import (
    ExtractFlowCommandsFromImageMetadataRequest,
    ExtractFlowCommandsFromImageMetadataResultFailure,
)
from griptape_nodes.retained_mode.file_metadata.workflow_metadata import FLOW_COMMANDS_KEY
from tests.unit.image.sample_images import encode, solid
from tests.unit.image.test_exif import _block, _entry

if TYPE_CHECKING:
    from io import BytesIO
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine


class TestReviewCompatibility:
    @pytest.mark.parametrize("invalid", [object(), None])
    def test_invalid_legacy_input_rejected(self, invalid: object) -> None:
        with (
            pytest.raises(TypeError, match="Unsupported image input"),
            opening_metadata_image(cast("MetadataImageInput", invalid)),
        ):
            pass

    def test_legacy_save_without_exif_keyword(self) -> None:
        image = MagicMock()
        image.format = "PNG"
        image.getexif.return_value = {1: "tag"}
        data = encode(solid(64, 64), "png")

        def save(buffer: BytesIO, **kwargs: object) -> None:
            if "exif" in kwargs:
                msg = "EXIF keyword unsupported"
                raise TypeError(msg)
            buffer.write(data)

        image.save.side_effect = save
        with opening_metadata_image(image) as opened:
            assert opened.width == 64  # noqa: PLR2004
        assert image.save.call_count == 2  # noqa: PLR2004

    @pytest.mark.parametrize(
        "raw", [b"JIS\x00\x00\x00\x00\x00" + "hello".encode("shift_jis"), b"\x00" * 8 + b"plain", b"\xffraw"]
    )
    def test_comment_prefixes(self, raw: bytes) -> None:
        parsed = Exif.from_bytes(_block("<", [], exif_ifd=[_entry(EXIF_USER_COMMENT_TAG, 7, len(raw), raw)]))
        assert parsed[EXIF_USER_COMMENT_TAG]

    def test_unreadable_workflow_text_reports_failure(self, engine: Engine, tmp_path: Path) -> None:
        source = encode(solid(64, 64), "png")
        chunk = png_chunk(b"zTXt", FLOW_COMMANDS_KEY.encode("latin-1") + b"\x00\x00invalid zlib")
        path = tmp_path / "unreadable.png"
        path.write_bytes(source[:-12] + chunk + source[-12:])
        result = engine.flow_manager.serialization.on_extract_flow_commands_from_image_metadata(
            ExtractFlowCommandsFromImageMetadataRequest(file_url_or_path=str(path), deserialize=False)
        )
        assert isinstance(result, ExtractFlowCommandsFromImageMetadataResultFailure)

    def test_long_unicode_text_uses_bounded_compression(self) -> None:
        assert should_compress_text("\u2603" * 2048)

    @pytest.mark.parametrize(
        "kind", ["missing", "directory", "corrupt", "metadata_limit", "download", "remote_corrupt"]
    )
    def test_flow_extraction_failures(self, engine: Engine, tmp_path: Path, kind: str) -> None:
        path = tmp_path / "source.png"
        source = str(path)
        context = patch("griptape_nodes.image.png_text_chunks.MAX_TOTAL_TEXT_BYTES", 1 << 26)
        if kind == "directory":
            path.mkdir()
        elif kind == "corrupt":
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(64))
        elif kind == "metadata_limit":
            path.write_bytes(insert_text_chunks(encode(solid(64, 64), "png"), {"key": "large"}))
            context = patch("griptape_nodes.image.png_text_chunks.MAX_TOTAL_TEXT_BYTES", 1)
        elif kind in {"download", "remote_corrupt"}:
            source = "https://example.test/image.png"
            response = MagicMock()
            response.content = b"not an image"
            if kind == "download":
                response.raise_for_status.side_effect = OSError("download failed")
            context = patch(
                "griptape_nodes.retained_mode.managers.flow.serialization.httpx2.get", return_value=response
            )
        with context:
            result = engine.flow_manager.serialization.on_extract_flow_commands_from_image_metadata(
                ExtractFlowCommandsFromImageMetadataRequest(file_url_or_path=source, deserialize=False)
            )
        assert isinstance(result, ExtractFlowCommandsFromImageMetadataResultFailure)
