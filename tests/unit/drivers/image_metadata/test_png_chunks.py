"""Shared PNG text compression policy."""

from PIL import PngImagePlugin

from griptape_nodes.drivers.image_metadata.png_chunks import should_compress_text


class TestShouldCompressText:
    def test_short_text_is_not_compressed(self) -> None:
        assert not should_compress_text("x" * 1024)

    def test_long_text_is_compressed(self) -> None:
        assert should_compress_text("x" * 1025)

    def test_text_over_pillow_decompress_limit_is_not_compressed(self) -> None:
        assert not should_compress_text("x" * (PngImagePlugin.MAX_TEXT_CHUNK + 1))
