import struct
import zlib
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image, PngImagePlugin

from griptape_nodes.drivers.image_metadata.png_chunks import PNG_SIGNATURE, read_png_text


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))


def _insert_after_idat(path: Path, chunks: bytes) -> None:
    data = path.read_bytes()
    path.write_bytes(data[:-12] + chunks + data[-12:])


class TestReadPngText:
    def test_reads_all_text_formats_before_and_after_pixels_without_changing_source(self, tmp_path: Path) -> None:
        path = tmp_path / "image.png"
        info = PngImagePlugin.PngInfo()
        expected = {
            "author": "Ren\u00e9",
            "workflow": "na\u00efve " * 2000,
            "unicode": "\u732b",
            "unicode_compressed": "\u732b\U0001f3a8" * 2000,
        }
        info.add_text("author", expected["author"])
        info.add_text("workflow", expected["workflow"], zip=True)
        info.add_itxt("unicode", expected["unicode"], lang="ja", tkey="\u540d\u524d")
        info.add_itxt("unicode_compressed", expected["unicode_compressed"], zip=True)
        Image.new("RGB", (16, 16), "red").save(path, pnginfo=info)
        _insert_after_idat(
            path,
            _chunk(b"tEXt", b"post_plain\x00Ren\xe9")
            + _chunk(b"zTXt", b"post_compressed\x00\x00" + zlib.compress(b"long " * 2000))
            + _chunk(b"iTXt", b"post_unicode\x00\x01\x00ja\x00\x00" + zlib.compress("\u732b".encode())),
        )
        expected.update(post_plain="Ren\u00e9", post_compressed="long " * 2000, post_unicode="\u732b")
        before = path.read_bytes()

        with patch.object(Image, "open", side_effect=AssertionError("Pixels must not be decoded")):
            actual = read_png_text(path)

        assert actual == expected
        assert all(type(value) is str for value in actual.values())
        assert path.read_bytes() == before

    def test_seeks_past_pixel_and_other_nontext_payloads(self, tmp_path: Path) -> None:
        path = tmp_path / "image.png"
        Image.new("RGB", (16, 16)).save(path)
        _insert_after_idat(path, _chunk(b"vpAg", b"nontext" * 1000) + _chunk(b"tEXt", b"prompt\x00kept"))
        data = path.read_bytes()
        forbidden_ranges = []
        position = len(PNG_SIGNATURE)
        while position < len(data):
            length = struct.unpack(">I", data[position : position + 4])[0]
            kind = data[position + 4 : position + 8]
            if kind in {b"IDAT", b"vpAg"}:
                forbidden_ranges.append(range(position + 8, position + 8 + length))
            position += length + 12

        class GuardedSource(BytesIO):
            def read(self, size: int = -1) -> bytes:
                start = self.tell()
                end = len(data)
                if size >= 0:
                    end = start + size
                for forbidden in forbidden_ranges:
                    if start < forbidden.stop and end > forbidden.start:
                        pytest.fail("Read a nontext payload instead of seeking over it")
                return super().read(size)

        with patch.object(Path, "open", return_value=GuardedSource(data)):
            actual = read_png_text(path)

        assert actual == {"prompt": "kept"}

    def test_expands_path_at_io_boundary(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = tmp_path / "image.png"
        Image.new("RGB", (1, 1)).save(path)
        monkeypatch.setenv("PNG_TEXT_DIR", str(tmp_path))

        actual = read_png_text(Path("$PNG_TEXT_DIR/image.png"))

        assert actual == {}

    def test_last_duplicate_value_wins_even_after_idat(self, tmp_path: Path) -> None:
        path = tmp_path / "image.png"
        info = PngImagePlugin.PngInfo()
        info.add_text("prompt", "old")
        Image.new("RGB", (1, 1)).save(path, pnginfo=info)
        _insert_after_idat(path, _chunk(b"tEXt", b"prompt\x00new"))

        actual = read_png_text(path)

        assert actual == {"prompt": "new"}

    @pytest.mark.parametrize("kind", [b"zTXt", b"iTXt"])
    def test_compressed_text_over_pillow_limit_is_rejected(self, tmp_path: Path, kind: bytes) -> None:
        path = tmp_path / "image.png"
        Image.new("RGB", (1, 1)).save(path)
        prefix = b"bomb\x00\x00"
        if kind == b"iTXt":
            prefix = b"bomb\x00\x01\x00\x00\x00"
        compressed = zlib.compress(b"x" * (PngImagePlugin.MAX_TEXT_CHUNK + 1))
        _insert_after_idat(path, _chunk(kind, prefix + compressed))
        before = path.read_bytes()

        with pytest.raises(ValueError, match=r"[Tt]ext|[Dd]ecompressed"):
            read_png_text(path)

        assert path.read_bytes() == before

    @pytest.mark.parametrize("kind", [b"zTXt", b"iTXt"])
    def test_compressed_text_at_pillow_limit_reads_back(self, tmp_path: Path, kind: bytes) -> None:
        path = tmp_path / "image.png"
        Image.new("RGB", (1, 1)).save(path)
        prefix = b"workflow\x00\x00"
        if kind == b"iTXt":
            prefix = b"workflow\x00\x01\x00\x00\x00"
        value = "x" * PngImagePlugin.MAX_TEXT_CHUNK
        _insert_after_idat(path, _chunk(kind, prefix + zlib.compress(value.encode())))

        actual = read_png_text(path)

        assert actual == {"workflow": value}

    def test_encoded_text_chunk_is_bounded_before_reading(self, tmp_path: Path) -> None:
        path = tmp_path / "image.png"
        Image.new("RGB", (1, 1)).save(path)
        _insert_after_idat(path, _chunk(b"tEXt", b"key\x00" + b"x" * 101))
        data = path.read_bytes()
        memory_limit = 100

        class BoundedSource(BytesIO):
            def read(self, size: int = -1) -> bytes:
                if size > memory_limit or size < 0:
                    pytest.fail("Attempted to allocate an oversized text chunk")
                return super().read(size)

        with (  # noqa: SIM117 - Keep the expected failure scoped to the read operation.
            patch.object(Path, "open", return_value=BoundedSource(data)),
            patch.object(PngImagePlugin, "MAX_TEXT_MEMORY", memory_limit),
        ):
            with pytest.raises(ValueError, match="text chunk"):
                read_png_text(path)

    def test_total_text_limit_includes_duplicate_keys(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = tmp_path / "image.png"
        Image.new("RGB", (1, 1)).save(path)
        _insert_after_idat(path, _chunk(b"tEXt", b"key\x00" + b"x" * 60) * 2)
        monkeypatch.setattr(PngImagePlugin, "MAX_TEXT_MEMORY", 100)

        with pytest.raises(ValueError, match=r"[Tt]ext|memory"):
            read_png_text(path)

    def test_large_uncompressed_text_is_not_subject_to_decompression_limit(self, tmp_path: Path) -> None:
        path = tmp_path / "image.png"
        info = PngImagePlugin.PngInfo()
        value = "x" * (PngImagePlugin.MAX_TEXT_CHUNK + 1)
        info.add_text("workflow", value)
        Image.new("RGB", (1, 1)).save(path, pnginfo=info)

        actual = read_png_text(path)

        assert actual == {"workflow": value}

    @pytest.mark.parametrize("cut", [1, 6, 15, 30])
    def test_truncated_png_is_rejected(self, tmp_path: Path, cut: int) -> None:
        path = tmp_path / "image.png"
        Image.new("RGB", (16, 16)).save(path)
        truncated = path.read_bytes()[:-cut]
        path.write_bytes(truncated)

        with pytest.raises(OSError, match=r"incomplete|truncated"):
            read_png_text(path)

        assert path.read_bytes() == truncated

    @pytest.mark.parametrize(
        ("kind", "payload"),
        [
            (b"tEXt", b"no separator"),
            (b"tEXt", b"\x00empty keyword"),
            (b"zTXt", b"key\x00\x02bad method"),
            (b"zTXt", b"key\x00\x00not compressed"),
            (b"zTXt", b"key\x00\x00" + zlib.compress(b"value")[:-1]),
            (b"iTXt", b"key\x00\x00"),
            (b"iTXt", b"key\x00\x02\x00\x00\x00value"),
            (b"iTXt", b"key\x00\x01\x01\x00\x00value"),
            (b"iTXt", b"key\x00\x00\x00\x00\x00\xff"),
            (b"iTXt", b"key\x00\x01\x00\x00\x00" + zlib.compress(b"value")[:-1]),
        ],
    )
    def test_invalid_text_is_rejected(self, tmp_path: Path, kind: bytes, payload: bytes) -> None:
        path = tmp_path / "image.png"
        Image.new("RGB", (1, 1)).save(path)
        _insert_after_idat(path, _chunk(kind, payload))

        with pytest.raises((OSError, ValueError), match=r"text|compressed|UTF|keyword"):
            read_png_text(path)

    def test_bad_text_checksum_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "image.png"
        Image.new("RGB", (1, 1)).save(path)
        chunk = _chunk(b"tEXt", b"key\x00value")
        _insert_after_idat(path, chunk[:-4] + b"\x00" * 4)

        with pytest.raises(OSError, match="checksum"):
            read_png_text(path)

    def test_non_png_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "image.png"
        path.write_bytes(b"not a PNG")

        with pytest.raises(OSError, match="not a PNG"):
            read_png_text(path)
