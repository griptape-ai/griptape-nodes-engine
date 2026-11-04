import struct
import zlib
from io import BytesIO

import pytest
from PIL import Image, PngImagePlugin

from griptape_nodes.drivers.image_metadata.png_metadata_driver import PngMetadataDriver


def _chunk_types(png: bytes) -> set[bytes]:
    """The PNG chunk types in ``png``, read from its chunk headers."""
    return set(_chunks(png))


def _chunks(png: bytes) -> dict[bytes, bytes]:
    """The PNG chunks in ``png`` by type, the last chunk of each type winning."""
    chunks = {}
    position = 8
    while position < len(png):
        length = int.from_bytes(png[position : position + 4], "big")
        chunks[png[position + 4 : position + 8]] = png[position + 8 : position + 8 + length]
        position += 12 + length
    return chunks


def _encode_chunk(chunk_type: bytes, chunk_data: bytes) -> bytes:
    return (
        struct.pack(">I", len(chunk_data))
        + chunk_type
        + chunk_data
        + struct.pack(">I", zlib.crc32(chunk_type + chunk_data))
    )


def _rgb16_png() -> bytes:
    """A 2x2 16-bit RGB PNG whose samples aren't multiples of 257, which Pillow can't write itself."""
    row = b"\x00" + struct.pack(">HHHHHH", 1000, 2000, 3001, 1001, 40000, 65534)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _encode_chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 16, 2, 0, 0, 0))
        + _encode_chunk(b"IDAT", zlib.compress(row * 2))
        + _encode_chunk(b"IEND", b"")
    )


def _rgb_png(pnginfo: PngImagePlugin.PngInfo | None = None) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (4, 4)).save(buffer, format="PNG", pnginfo=pnginfo)
    return buffer.getvalue()


def _inject(png: bytes, metadata: dict[str, str]) -> bytes:
    return PngMetadataDriver().inject_metadata(png, Image.open(BytesIO(png)), metadata)


class TestPngMetadataDriver:
    def test_keeps_16_bit_pixel_data_unchanged(self) -> None:
        source = _rgb16_png()

        png = _inject(source, {"gtn_short": "x"})

        assert _chunks(png)[b"IHDR"] == _chunks(source)[b"IHDR"]
        assert _chunks(png)[b"IDAT"] == _chunks(source)[b"IDAT"]
        assert PngMetadataDriver().extract_metadata(Image.open(BytesIO(png))) == {"gtn_short": "x"}

    def test_replaces_existing_key_and_keeps_other_text(self) -> None:
        existing = PngImagePlugin.PngInfo()
        existing.add_text("gtn_short", "old")
        existing.add_text("Author", "someone")

        png = _inject(_rgb_png(existing), {"gtn_short": "new"})

        assert PngMetadataDriver().extract_metadata(Image.open(BytesIO(png))) == {
            "Author": "someone",
            "gtn_short": "new",
        }

    def test_keeps_bytes_after_iend(self) -> None:
        source = _rgb_png() + b"\x00\x00\x00\x00"

        png = _inject(source, {"gtn_short": "x"})

        assert png.endswith(b"IEND\xaeB`\x82\x00\x00\x00\x00")
        assert PngMetadataDriver().extract_metadata(Image.open(BytesIO(png))) == {"gtn_short": "x"}

    def test_rejects_data_that_is_not_png(self) -> None:
        with pytest.raises(ValueError, match="PNG signature"):
            PngMetadataDriver().inject_metadata(b"not a png", Image.new("RGB", (4, 4)), {"gtn_short": "x"})

    def test_rejects_truncated_png(self) -> None:
        source = _rgb_png()

        with pytest.raises(ValueError, match="ends partway"):
            PngMetadataDriver().inject_metadata(source[:-6], Image.open(BytesIO(source)), {"gtn_short": "x"})

    def test_long_text_is_compressed_and_reads_back(self) -> None:
        long_text = '{"nodes": [' + ", ".join(['{"name": "node"}'] * 200) + "]}"

        png = _inject(_rgb_png(), {"gtn_long": long_text, "gtn_short": "x"})

        assert b"zTXt" in _chunk_types(png)
        assert len(png) < len(long_text)
        assert PngMetadataDriver().extract_metadata(Image.open(BytesIO(png))) == {
            "gtn_long": long_text,
            "gtn_short": "x",
        }

    def test_short_text_stays_uncompressed(self) -> None:
        png = _inject(_rgb_png(), {"gtn_short": "x"})

        assert b"zTXt" not in _chunk_types(png)
        assert b"tEXt" in _chunk_types(png)

    def test_text_over_pillow_decompress_limit_stays_uncompressed_and_reads_back(self) -> None:
        huge_text = "a" * (PngImagePlugin.MAX_TEXT_CHUNK + 1)

        png = _inject(_rgb_png(), {"gtn_huge": huge_text})

        assert b"zTXt" not in _chunk_types(png)
        assert b"tEXt" in _chunk_types(png)
        assert PngMetadataDriver().extract_metadata(Image.open(BytesIO(png))) == {"gtn_huge": huge_text}

    def test_text_under_pillow_decompress_limit_is_compressed(self) -> None:
        large_text = "a" * (PngImagePlugin.MAX_TEXT_CHUNK - 1)

        png = _inject(_rgb_png(), {"gtn_large": large_text})

        assert b"zTXt" in _chunk_types(png)
        assert PngMetadataDriver().extract_metadata(Image.open(BytesIO(png))) == {"gtn_large": large_text}
