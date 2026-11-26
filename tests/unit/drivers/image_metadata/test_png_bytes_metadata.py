"""Metadata byte writes preserve PNG encoding and legacy driver compatibility."""

import logging
import struct
import zlib
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image, PngImagePlugin

from griptape_nodes.drivers.image_metadata.base_image_metadata_driver import BaseImageMetadataDriver
from griptape_nodes.drivers.image_metadata.image_metadata_driver_registry import ImageMetadataDriverRegistry
from griptape_nodes.drivers.image_metadata.png_metadata_driver import PngMetadataDriver
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.artifact_events import (
    RegisterArtifactProviderRequest,
    RegisterArtifactProviderResultSuccess,
)
from griptape_nodes.retained_mode.events.os_events import WriteFileRequest, WriteFileResultSuccess
from griptape_nodes.retained_mode.managers.artifact_providers.image.image_artifact_provider import ImageArtifactProvider
from griptape_nodes.retained_mode.managers.artifact_providers.provider_registry import ProviderRegistry
from tests.workflow_context import unsaved_workflow_open

RGB_CHANNELS = 3
TEXT_KINDS = {b"tEXt", b"zTXt", b"iTXt"}
PROVIDER_MODULE = "griptape_nodes.retained_mode.managers.artifact_providers.image.image_artifact_provider"


def _chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))


def _png16(channels: int) -> bytes:
    if channels == RGB_CHANNELS:
        color_type = 2
    else:
        color_type = 6
    header = struct.pack(">IIBBBBB", 2, 1, 16, color_type, 0, 0, 0)
    pixels = b"\x00" + b"\x40\x01\x80\x02\xc0\x03\xff\xfe"[: channels * 2] * 2
    compressed = zlib.compress(pixels)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"pHYs", struct.pack(">IIB", 1000, 1000, 1))
        + _chunk(b"vpAg", b"private ancillary data")
        + _chunk(b"tEXt", b"gtn_workflow_name\x00OLD")
        + _chunk(b"tEXt", b"author\x00someone")
        + _chunk(b"IDAT", compressed[:5])
        + _chunk(b"IDAT", compressed[5:])
        + _chunk(b"IEND", b"")
    )


def _chunks(data: bytes) -> list[bytes]:
    chunks = []
    offset = 8
    while offset < len(data):
        length = int.from_bytes(data[offset : offset + 4], "big")
        chunks.append(data[offset : offset + length + 12])
        offset += length + 12
    return chunks


def _non_text(data: bytes) -> list[bytes]:
    return [chunk for chunk in _chunks(data) if chunk[4:8] not in TEXT_KINDS]


def _text(data: bytes) -> dict[str, str]:
    with BytesIO(data) as source, Image.open(source) as image:
        return PngMetadataDriver().extract_metadata(image)


class LegacyDriver(BaseImageMetadataDriver):
    """A custom driver implementing only the original Pillow interface."""

    def __init__(self) -> None:
        self.formats_seen: list[str | None] = []
        self.source: BytesIO | None = None

    def get_supported_formats(self) -> list[str]:
        return ["PNG", "JPEG"]

    def inject_metadata(self, pil_image: Image.Image, metadata: dict[str, str]) -> bytes:
        self.formats_seen.append(pil_image.format)
        self.source = pil_image.fp  # type: ignore[attr-defined]
        return metadata["result"].encode()

    def extract_metadata(self, _pil_image: Image.Image) -> dict[str, str]:
        return {}


class FailingLegacyDriver(LegacyDriver):
    def inject_metadata(self, pil_image: Image.Image, _metadata: dict[str, str]) -> bytes:
        self.source = pil_image.fp  # type: ignore[attr-defined]
        msg = "custom driver failed"
        raise RuntimeError(msg)


@pytest.mark.parametrize("channels", [3, 4], ids=["rgb16", "rgba16"])
def test_bytes_hook_preserves_all_non_text_chunks(channels: int) -> None:
    """Keep IHDR, ancillary chunks and split IDATs byte-for-byte intact."""
    original = _png16(channels)

    result = PngMetadataDriver().inject_metadata_into_bytes(original, {"gtn_workflow_name": "NEW"})

    text = _text(result)
    assert _non_text(result) == _non_text(original)
    chunks = _chunks(result)
    assert chunks[0][4:8] == b"IHDR"
    assert chunks[1][4:8] == b"tEXt"
    assert chunks[1][8:].startswith(b"gtn_workflow_name\x00")
    assert text == {"gtn_workflow_name": "NEW", "author": "someone"}
    assert sum(chunk[4:8] == b"tEXt" for chunk in _chunks(result)) == len(text)


@pytest.mark.parametrize("compressed", [False, True])
def test_bytes_hook_replaces_all_text_encodings_and_duplicates(*, compressed: bool) -> None:
    """Replace duplicate Latin-1 and international text without rewriting unrelated text."""
    info = PngImagePlugin.PngInfo()
    info.add_text("latin", "old", zip=compressed)
    info.add_text("unicode", "\u732b", zip=compressed)
    info.add_text("latin", "duplicate", zip=compressed)
    info.add_text("unrelated", "\u00e9 \u732b", zip=compressed)
    with BytesIO() as source, Image.new("RGB", (2, 2)) as image:
        image.save(source, format="PNG", pnginfo=info)
        original = source.getvalue()

    result = PngMetadataDriver().inject_metadata_into_bytes(original, {"latin": "new", "unicode": "new2"})

    text = _text(result)
    assert text == {"latin": "new", "unicode": "new2", "unrelated": "\u00e9 \u732b"}
    assert len([chunk for chunk in _chunks(result) if chunk[4:8] in TEXT_KINDS]) == len(text)
    original_unrelated = [chunk for chunk in _chunks(original) if chunk[8:].startswith(b"unrelated\x00")]
    result_unrelated = [chunk for chunk in _chunks(result) if chunk[8:].startswith(b"unrelated\x00")]
    assert result_unrelated == original_unrelated


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("short", b"tEXt"),
        ("x" * 2000, b"zTXt"),
        ("\u732b" * 2000, b"iTXt"),
        ("x" * (PngImagePlugin.MAX_TEXT_CHUNK + 1), b"tEXt"),
    ],
    ids=["short-text", "compressed-text", "unicode-text", "above-compression-limit"],
)
def test_bytes_hook_compression_and_unicode(value: str, kind: bytes) -> None:
    """Use the shared compression policy and round-trip Unicode text."""
    result = PngMetadataDriver().inject_metadata_into_bytes(_png16(3), {"new": value})

    added = [chunk for chunk in _chunks(result) if chunk[8:].startswith(b"new\x00")]
    assert len(added) == 1
    assert added[0][4:8] == kind
    text = _text(result)
    assert text["new"] == value
    if kind == b"iTXt":
        assert added[0][12] == 1


@pytest.mark.parametrize("key", ["", "nul\x00key", "k" * 80, "\u732b"])
def test_bytes_hook_rejects_invalid_keyword(key: str) -> None:
    """Reject keys that cannot be encoded as PNG keywords."""
    with pytest.raises(ValueError, match="keyword"):
        PngMetadataDriver().inject_metadata_into_bytes(_png16(3), {key: "x"})


@pytest.mark.parametrize("data", [b"not a PNG", _png16(3)[:12], _png16(3)[:60], _png16(3)[:-15]])
def test_bytes_hook_rejects_invalid_or_incomplete_png(data: bytes) -> None:
    """Refuse non-PNG data and truncated chunk headers or bodies."""
    with pytest.raises(OSError, match=r"not a PNG|incomplete"):
        PngMetadataDriver().inject_metadata_into_bytes(data, {"new": "x"})


def test_bytes_hook_empty_metadata_returns_original() -> None:
    """Skip rewriting when no metadata was supplied."""
    original = _png16(3)
    result = PngMetadataDriver().inject_metadata_into_bytes(original, {})
    assert result is original


def test_base_bytes_hook_supports_legacy_driver_and_closes_source() -> None:
    """Delegate to the original interface and release the input buffer."""
    driver = LegacyDriver()
    result = driver.inject_metadata_into_bytes(_png16(3), {"result": "custom"})
    assert result == b"custom"
    assert driver.formats_seen == ["PNG"]
    assert driver.source is not None
    assert driver.source.closed


def test_base_bytes_hook_closes_source_when_legacy_driver_fails() -> None:
    """Release the input buffer even when a custom driver raises."""
    driver = FailingLegacyDriver()
    with pytest.raises(RuntimeError, match="custom driver failed"):
        driver.inject_metadata_into_bytes(_png16(3), {"result": "custom"})
    assert driver.source is not None
    assert driver.source.closed


@pytest.mark.parametrize("actual_format", ["PNG", "JPEG"])
def test_provider_uses_actual_format_and_registered_legacy_driver(actual_format: str) -> None:
    """Choose the first registered driver using image headers, not the file extension."""
    with BytesIO() as source, Image.new("RGB", (2, 2)) as image:
        image.save(source, format=actual_format)
        original = source.getvalue()
    driver = LegacyDriver()
    provider = ImageArtifactProvider(ProviderRegistry())
    with patch.object(ImageMetadataDriverRegistry, "_drivers", [driver, PngMetadataDriver()]):
        result = provider.prepare_content_for_write(original, "misleading.png", {"result": "custom"})
    assert result == b"custom"
    assert driver.formats_seen == [actual_format]
    assert driver.source is not None
    assert driver.source.closed


@pytest.mark.parametrize("data", [b"not a PNG", _png16(3)[:-15]], ids=["invalid", "incomplete"])
def test_provider_rejects_bad_image_without_changing_bytes(data: bytes, caplog: pytest.LogCaptureFixture) -> None:
    """Leave unreadable image bytes alone and warn the caller."""
    provider = ImageArtifactProvider(ProviderRegistry())
    with (
        patch.object(ImageMetadataDriverRegistry, "_drivers", [PngMetadataDriver()]),
        caplog.at_level(logging.WARNING),
    ):
        result = provider.prepare_content_for_write(data, "image.png", {"new": "x"})
    assert result is data
    assert "Failed because:" in caplog.text


def test_provider_missing_driver_warns_and_returns_original(caplog: pytest.LogCaptureFixture) -> None:
    """A format without a driver is a best-effort skip."""
    original = _png16(3)
    provider = ImageArtifactProvider(ProviderRegistry())
    with patch.object(ImageMetadataDriverRegistry, "_drivers", []), caplog.at_level(logging.WARNING):
        result = provider.prepare_content_for_write(original, "image.png", {"new": "x"})
    assert result is original
    assert "No metadata driver found for format: PNG" in caplog.text


@pytest.mark.parametrize("failure", [ValueError("bad key"), RuntimeError("custom driver failed")])
def test_provider_bytes_hook_failure_warns_and_returns_original(
    failure: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    """Keep original bytes on built-in or custom driver failures."""
    original = _png16(3)
    provider = ImageArtifactProvider(ProviderRegistry())
    driver = PngMetadataDriver()
    with (
        patch.object(ImageMetadataDriverRegistry, "_drivers", [driver]),
        patch.object(driver, "inject_metadata_into_bytes", side_effect=failure),
        caplog.at_level(logging.WARNING),
    ):
        result = provider.prepare_content_for_write(original, "image.png", {"new": "x"})
    assert result is original
    assert "Attempted to inject metadata into image.png" in caplog.text


@pytest.mark.parametrize("channels", [3, 4], ids=["rgb16", "rgba16"])
def test_ordinary_write_file_request_preserves_png16_with_bound_metadata(
    engine: Engine, tmp_path: Path, channels: int
) -> None:
    """Dispatch a normal byte write with workflow-bound metadata and retain PNG16 encoding."""
    original = _png16(channels)
    registration = engine.handle_request(RegisterArtifactProviderRequest(provider_class=ImageArtifactProvider))
    assert isinstance(registration, RegisterArtifactProviderResultSuccess)
    path = tmp_path / "written.png"
    with (
        unsaved_workflow_open(engine),
        patch.object(ImageMetadataDriverRegistry, "_drivers", [PngMetadataDriver()]),
        patch(f"{PROVIDER_MODULE}.collect_workflow_metadata", return_value={"gtn_workflow_name": "BOUND"}) as collect,
    ):
        workflow = engine.current_workflow()
        result = engine.handle_request(WriteFileRequest(file_path=str(path), content=original))
        collect.assert_called_once_with(engine, workflow)
    assert isinstance(result, WriteFileResultSuccess)
    written = Path(result.final_file_path).read_bytes()
    text = _text(written)
    chunks = _chunks(written)
    header = struct.unpack(">IIBBBBB", chunks[0][8:-4])
    if channels == RGB_CHANNELS:
        expected_color_type = 2
    else:
        expected_color_type = 6
    assert header == (2, 1, 16, expected_color_type, 0, 0, 0)
    compressed_pixels = b"".join(chunk[8:-4] for chunk in chunks if chunk[4:8] == b"IDAT")
    scanline = zlib.decompress(compressed_pixels)
    assert scanline[0] == 0  # The fixture uses an unfiltered scanline.
    pixels = struct.unpack(f">{channels * 2}H", scanline[1:])
    expected_pixel = [16385, 32770, 49155]
    if channels != RGB_CHANNELS:
        expected_pixel.append(65534)
    assert list(pixels) == expected_pixel * 2
    assert _non_text(written) == _non_text(original)
    assert text == {"gtn_workflow_name": "BOUND", "author": "someone"}
