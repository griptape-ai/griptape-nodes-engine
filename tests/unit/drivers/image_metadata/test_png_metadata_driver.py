from io import BytesIO

import pytest
from PIL import Image, PngImagePlugin

from griptape_nodes.drivers.image_metadata.png_metadata_driver import PngMetadataDriver
from griptape_nodes.image import IMAGE_BACKEND_ENV_VAR, OPENCV_BACKEND, PILLOW_BACKEND, GriptapeImage, open_image


@pytest.fixture(autouse=True, params=[PILLOW_BACKEND, OPENCV_BACKEND])
def image_backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run the test once per image backend."""
    monkeypatch.setenv(IMAGE_BACKEND_ENV_VAR, request.param)
    return request.param


def _blank_png() -> GriptapeImage:
    buffer = BytesIO()
    Image.new("RGB", (4, 4)).save(buffer, format="PNG")
    return open_image(buffer.getvalue())


def _chunk_types(png: bytes) -> set[bytes]:
    """The PNG chunk types in ``png``, read from its chunk headers."""
    types = set()
    position = 8
    while position < len(png):
        length = int.from_bytes(png[position : position + 4], "big")
        types.add(png[position + 4 : position + 8])
        position += 12 + length
    return types


class TestPngMetadataDriver:
    def test_long_text_is_compressed_and_reads_back(self) -> None:
        long_text = '{"nodes": [' + ", ".join(['{"name": "node"}'] * 200) + "]}"

        png = PngMetadataDriver().inject_metadata(_blank_png(), {"gtn_long": long_text, "gtn_short": "x"})

        assert b"zTXt" in _chunk_types(png)
        assert len(png) < len(long_text)
        assert PngMetadataDriver().extract_metadata(open_image(png)) == {
            "gtn_long": long_text,
            "gtn_short": "x",
        }

    def test_short_text_stays_uncompressed(self) -> None:
        png = PngMetadataDriver().inject_metadata(_blank_png(), {"gtn_short": "x"})

        assert b"zTXt" not in _chunk_types(png)
        assert b"tEXt" in _chunk_types(png)

    def test_text_over_pillow_decompress_limit_stays_uncompressed_and_reads_back(self) -> None:
        huge_text = "a" * (PngImagePlugin.MAX_TEXT_CHUNK + 1)

        png = PngMetadataDriver().inject_metadata(_blank_png(), {"gtn_huge": huge_text})

        assert b"zTXt" not in _chunk_types(png)
        assert b"tEXt" in _chunk_types(png)
        assert PngMetadataDriver().extract_metadata(open_image(png)) == {"gtn_huge": huge_text}

    def test_text_under_pillow_decompress_limit_is_compressed(self) -> None:
        large_text = "a" * (PngImagePlugin.MAX_TEXT_CHUNK - 1)

        png = PngMetadataDriver().inject_metadata(_blank_png(), {"gtn_large": large_text})

        assert b"zTXt" in _chunk_types(png)
        assert PngMetadataDriver().extract_metadata(open_image(png)) == {"gtn_large": large_text}
