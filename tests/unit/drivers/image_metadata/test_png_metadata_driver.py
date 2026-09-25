from io import BytesIO

from PIL import Image

from griptape_nodes.drivers.image_metadata.png_metadata_driver import PngMetadataDriver


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

        png = PngMetadataDriver().inject_metadata(Image.new("RGB", (4, 4)), {"gtn_long": long_text, "gtn_short": "x"})

        assert b"zTXt" in _chunk_types(png)
        assert len(png) < len(long_text)
        assert PngMetadataDriver().extract_metadata(Image.open(BytesIO(png))) == {
            "gtn_long": long_text,
            "gtn_short": "x",
        }

    def test_short_text_stays_uncompressed(self) -> None:
        png = PngMetadataDriver().inject_metadata(Image.new("RGB", (4, 4)), {"gtn_short": "x"})

        assert b"zTXt" not in _chunk_types(png)
        assert b"tEXt" in _chunk_types(png)
