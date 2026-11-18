"""Tests for expanded OpenImageIO format support, lazy file-header reads, and ImageCache usage."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import OpenImageIO as oiio  # noqa: N813
import pytest

from griptape_nodes.image import (
    GriptapeImage,
    GriptapeImagePipeline,
    ImageFormat,
)
from griptape_nodes.image.formats import sniff_image_extension
from griptape_nodes.image.oiio_config import apply_openimageio_settings

if TYPE_CHECKING:
    from collections.abc import Generator

    from pytest_mock import MockerFixture

    from griptape_nodes.retained_mode.engine import Engine


@pytest.fixture(autouse=True)
def reset_shared_cache(engine: Engine) -> Generator[None, None, None]:
    """Reset OpenImageIO settings and clear shared ImageCache entries around each test."""
    apply_openimageio_settings(engine.config_manager)
    oiio.ImageCache(shared=True).invalidate_all(True)
    yield
    oiio.ImageCache(shared=True).invalidate_all(True)


def _minimal_psd_bytes(width: int = 4, height: int = 4) -> bytes:
    return (
        b"8BPS"
        + struct.pack(">H6xHIIHH", 1, 3, height, width, 8, 3)
        + struct.pack(">IIIH", 0, 0, 0, 0)
        + b"\x80" * (width * height * 3)
    )


def _minimal_dds_bytes(width: int = 4, height: int = 4) -> bytes:
    pixel_format = struct.pack("<8I", 32, 0x41, 0, 32, 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
    header = (
        struct.pack("<7I44x", 124, 0x1007, height, width, width * 4, 0, 0)
        + pixel_format
        + struct.pack("<5I", 0x1000, 0, 0, 0, 0)
    )
    return b"DDS " + header + b"\xff\x40\x20\xff" * (width * height)


def _minimal_cineon_bytes(width: int = 4, height: int = 4) -> bytes:
    header = bytearray(2048)
    struct.pack_into(">IIII", header, 0, 0x802A5FD7, 2048, 192, 680)
    struct.pack_into(">I", header, 16, 2048 + width * height * 4)
    header[192] = 0
    header[193] = 3
    for channel in range(3):
        offset = 196 + channel * 28
        header[offset + 1] = channel + 1
        header[offset + 2] = 8
        struct.pack_into(">IIffff", header, offset + 4, width, height, 0.0, 0.0, 255.0, 1.0)
    header.extend(b"\x80" * (width * height * 4))
    return bytes(header)


def _oiio_file_bytes(tmp_path: Path, suffix: str, *, is_float: bool = False, grayscale: bool = False) -> bytes:
    channels = 1 if grayscale else 3
    if is_float:
        pixels = np.full((32, 32, channels), 0.5, dtype=np.float32)
    else:
        pixels = np.full((32, 32, channels), 128, dtype=np.uint8)
    target = tmp_path / f"sample{suffix}"
    buf = oiio.ImageBuf(np.ascontiguousarray(pixels))
    assert buf.write(str(target)), buf.geterror()
    return target.read_bytes()


class TestExpandedFormatSniffing:
    @pytest.mark.parametrize(
        ("factory_kind", "expected_ext", "expected_format"),
        [
            ("exr", "exr", ImageFormat.EXR),
            ("hdr", "hdr", ImageFormat.HDR),
            ("dpx", "dpx", ImageFormat.DPX),
            ("cin", "cin", ImageFormat.CIN),
            ("psd", "psd", ImageFormat.PSD),
            ("jp2", "jp2", ImageFormat.JP2),
            ("pfm", "pfm", ImageFormat.PFM),
            ("ppm", "ppm", ImageFormat.PPM),
            ("pgm", "pgm", ImageFormat.PGM),
            ("sgi", "sgi", ImageFormat.SGI),
            ("dds", "dds", ImageFormat.DDS),
        ],
    )
    def test_sniffs_and_opens_expanded_formats(
        self,
        tmp_path: Path,
        factory_kind: str,
        expected_ext: str,
        expected_format: ImageFormat,
    ) -> None:
        if factory_kind == "psd":
            payload = _minimal_psd_bytes()
        elif factory_kind == "dds":
            payload = _minimal_dds_bytes()
        elif factory_kind == "cin":
            payload = _minimal_cineon_bytes()
        else:
            payload = _oiio_file_bytes(
                tmp_path,
                f".{factory_kind}",
                is_float=factory_kind in ("exr", "hdr", "pfm"),
                grayscale=factory_kind == "pgm",
            )

        assert sniff_image_extension(payload) == expected_ext
        with GriptapeImage.open(payload) as img:
            assert img.format == expected_format
            assert img.width > 0
            assert img.height > 0
            if factory_kind != "cin":
                preview_bytes = GriptapeImagePipeline(img).thumbnail(16, 16).end().encode(ImageFormat.WEBP)
                assert sniff_image_extension(preview_bytes) == "webp"

    def test_from_name_resolves_oiiio_format_aliases(self) -> None:
        assert ImageFormat.from_name("openexr") == ImageFormat.EXR
        assert ImageFormat.from_name("cineon") == ImageFormat.CIN
        assert ImageFormat.from_name("jpeg2000") == ImageFormat.JP2
        assert ImageFormat.from_name("pbm") == ImageFormat.PBM

    @pytest.mark.parametrize("target_format", [ImageFormat.WEBP, ImageFormat.PNG, ImageFormat.JPEG])
    def test_multichannel_exr_encodes_to_preview_formats(self, tmp_path: Path, target_format: ImageFormat) -> None:
        # Given a 6-channel float EXR (RGBA + depth + object id)
        pixels = np.full((16, 16, 6), 0.5, dtype=np.float32)
        exr_path = tmp_path / "beauty_with_aovs.exr"
        buf = oiio.ImageBuf(np.ascontiguousarray(pixels))
        assert buf.write(str(exr_path)), buf.geterror()

        # When thumbnailing and encoding to an 8-bit preview format
        with GriptapeImage.open(exr_path) as img:
            encoded = GriptapeImagePipeline(img).thumbnail(8, 8).end().encode(target_format)

        # Then encoding succeeds and produces valid preview bytes
        assert sniff_image_extension(encoded) == target_format.extension


class TestLazyHeaderAndImageCacheRouting:
    def test_opening_path_and_reading_header_does_not_slurp_whole_file(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        # Given an EXR file on disk
        exr_path = tmp_path / "render.exr"
        exr_path.write_bytes(_oiio_file_bytes(tmp_path, ".exr", is_float=True))
        read_bytes_spy = mocker.spy(Path, "read_bytes")

        # When opening from Path and reading header dimensions, channels, format, and orientation
        with GriptapeImage.open(exr_path) as img:
            assert img.format == ImageFormat.EXR
            assert img.width == 32  # noqa: PLR2004
            assert img.height == 32  # noqa: PLR2004
            assert img.channels == 3  # noqa: PLR2004
            assert img.oriented_size == (32, 32)

        # Then Path.read_bytes() was never called
        assert read_bytes_spy.call_count == 0

    def test_decoding_from_path_routes_through_shared_image_cache(self, tmp_path: Path) -> None:
        # Given a local image file and recorded ImageCache counters
        cache = oiio.ImageCache(shared=True)
        exr_path = tmp_path / "cached_frame.exr"
        exr_path.write_bytes(_oiio_file_bytes(tmp_path, ".exr", is_float=True))
        cache.invalidate_all(True)
        unique_before = cache.getattribute("stat:unique_files", oiio.INT)
        bytes_before = cache.getattribute("stat:bytes_read", oiio.INT64)

        # When a thumbnail pipeline decodes from the Path
        with GriptapeImage.open(exr_path) as img:
            preview = GriptapeImagePipeline(img).thumbnail(16, 16).end()
            _ = preview.encode(ImageFormat.WEBP)

        # Then the file was opened and read through the shared ImageCache
        bytes_after_cold = cache.getattribute("stat:bytes_read", oiio.INT64)
        assert cache.getattribute("stat:unique_files", oiio.INT) == unique_before + 1
        assert bytes_after_cold > bytes_before

        # And a second thumbnail from the same Path reuses cached pixels with zero extra disk reads
        with GriptapeImage.open(exr_path) as img_warm:
            preview_warm = GriptapeImagePipeline(img_warm).thumbnail(16, 16).end()
            _ = preview_warm.encode(ImageFormat.WEBP)
        assert cache.getattribute("stat:bytes_read", oiio.INT64) == bytes_after_cold

    def test_decoding_from_ephemeral_bytes_leaves_no_cached_tiles_or_open_handles(self, tmp_path: Path) -> None:
        # Given raw image bytes in memory and an invalidated shared ImageCache
        cache = oiio.ImageCache(shared=True)
        raw_bytes = _oiio_file_bytes(tmp_path, ".png")
        cache.invalidate_all(True)

        # When decoding and thumbnailing from raw bytes
        with GriptapeImage.open(raw_bytes) as img:
            preview = GriptapeImagePipeline(img).thumbnail(16, 16).end()
            _ = preview.encode(ImageFormat.WEBP)

        # Then no temporary file handle or tile remains in the shared ImageCache
        stats = cache.getstats(1)
        assert "No images opened" in stats or "0 current" in stats
        assert "Tiles : 0 B" in stats
