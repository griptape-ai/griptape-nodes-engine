"""Tests for PngMetadataDriver on OpenImageIO images, raw bytes, and duck-typed legacy image objects."""

from __future__ import annotations

from typing import TYPE_CHECKING, BinaryIO

from griptape_nodes.drivers.image_metadata.png_metadata_driver import PngMetadataDriver
from griptape_nodes.image import GriptapeImage
from griptape_nodes.image.png_text_chunks import MAX_COMPRESSED_TEXT_BYTES
from tests.unit.image.sample_images import RED, encode, solid

if TYPE_CHECKING:
    from pathlib import Path


def _chunk_types(png: bytes) -> set[bytes]:
    """Return the PNG chunk types present in ``png``."""
    types = set()
    position = 8
    while position < len(png):
        length = int.from_bytes(png[position : position + 4], "big")
        types.add(png[position + 4 : position + 8])
        position += 12 + length
    return types


class _LegacyDuckPilPng:
    """Simulates a PIL.Image.Image passed by older griptape-nodes-library-standard nodes without importing PIL."""

    def __init__(self, png_bytes: bytes, info: dict[str, object] | None = None) -> None:
        self._png_bytes = png_bytes
        self.format = "PNG"
        self.info: dict[str, object] = dict(info or {})

    def save(self, fp: BinaryIO, format: str | None = None, **_kwargs: object) -> None:  # noqa: A002
        assert format == "PNG"
        fp.write(self._png_bytes)


class TestPngMetadataDriver:
    def test_long_text_is_compressed_and_reads_back(self) -> None:
        long_text = '{"nodes": [' + ", ".join(['{"name": "node"}'] * 200) + "]}"
        raw_png = encode(solid(4, 4, RED), "png")

        with GriptapeImage.open(raw_png) as source:
            png = PngMetadataDriver().inject_metadata(source, {"gtn_long": long_text, "gtn_short": "x"})

        assert b"zTXt" in _chunk_types(png)
        assert len(png) < len(long_text)
        with GriptapeImage.open(png) as reopened:
            assert PngMetadataDriver().extract_metadata(reopened) == {
                "gtn_long": long_text,
                "gtn_short": "x",
            }

    def test_short_text_stays_uncompressed(self) -> None:
        raw_png = encode(solid(4, 4, RED), "png")

        png = PngMetadataDriver().inject_metadata(raw_png, {"gtn_short": "x"})

        assert b"zTXt" not in _chunk_types(png)
        assert b"tEXt" in _chunk_types(png)

    def test_text_over_decompress_limit_stays_uncompressed_and_reads_back(self) -> None:
        huge_text = "a" * (MAX_COMPRESSED_TEXT_BYTES + 1)
        raw_png = encode(solid(4, 4, RED), "png")

        png = PngMetadataDriver().inject_metadata(raw_png, {"gtn_huge": huge_text})

        assert b"zTXt" not in _chunk_types(png)
        assert b"tEXt" in _chunk_types(png)
        assert PngMetadataDriver().extract_metadata(png) == {"gtn_huge": huge_text}

    def test_text_under_decompress_limit_is_compressed(self) -> None:
        large_text = "a" * (MAX_COMPRESSED_TEXT_BYTES - 1)
        raw_png = encode(solid(4, 4, RED), "png")

        png = PngMetadataDriver().inject_metadata(raw_png, {"gtn_large": large_text})

        assert b"zTXt" in _chunk_types(png)
        assert PngMetadataDriver().extract_metadata(png) == {"gtn_large": large_text}

    def test_accepts_path_and_preserves_existing_text_chunks(self, tmp_path: Path) -> None:
        initial = PngMetadataDriver().inject_metadata(encode(solid(4, 4, RED), "png"), {"existing": "keep", "k": "old"})
        png_path = tmp_path / "meta.png"
        png_path.write_bytes(initial)

        updated = PngMetadataDriver().inject_metadata(png_path, {"k": "new", "added": "val"})

        assert PngMetadataDriver().extract_metadata(updated) == {
            "existing": "keep",
            "k": "new",
            "added": "val",
        }

    def test_legacy_duck_typed_pil_object_preserves_info_text_without_pil(self) -> None:
        # Given a legacy object with .format, .info, and .save() like PIL.Image.Image
        raw_png = encode(solid(4, 4, RED), "png")
        legacy_obj = _LegacyDuckPilPng(raw_png, info={"Author": "Dan", "binary_ignored": b"\x00\x01"})

        # When extracting and injecting metadata through PngMetadataDriver
        extracted = PngMetadataDriver().extract_metadata(legacy_obj)
        injected = PngMetadataDriver().inject_metadata(legacy_obj, {"Shot": "sh010"})

        # Then string info entries are preserved and merged without importing PIL
        assert extracted == {"Author": "Dan"}
        assert PngMetadataDriver().extract_metadata(injected) == {"Author": "Dan", "Shot": "sh010"}
