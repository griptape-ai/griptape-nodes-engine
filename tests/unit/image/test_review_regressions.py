"""Regression coverage for the OpenImageIO migration review."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import OpenImageIO as oiio  # noqa: N813
import pytest

from griptape_nodes.drivers.image_metadata.exif_metadata_driver import ExifMetadataDriver
from griptape_nodes.image import Exif, GriptapeImage, GriptapeImagePipeline, ImageFormat
from griptape_nodes.image.image_io import write_file
from tests.unit.image.sample_images import RED, encode, noise, solid


@pytest.mark.parametrize("extension", ["bmp", "sgi", "dpx"])
@pytest.mark.parametrize("use_path", [False, True])
def test_straight_alpha_formats_keep_stored_color(extension: str, tmp_path: Path, *, use_path: bool) -> None:
    """Straight-alpha codecs preserve color on both decode paths."""
    pixels = solid(64, 64, (100, 60, 20, 128))
    data = encode(pixels, extension)
    path = tmp_path / f"source.{extension}"
    path.write_bytes(data)
    source: bytes | Path = data
    if use_path:
        source = path
    with GriptapeImage.open(source) as image:
        actual = image._decoded().get_pixels(oiio.UINT8)
        assert isinstance(actual, np.ndarray)
        np.testing.assert_allclose(actual, pixels, atol=1)


def test_exr_writer_stores_associated_alpha(tmp_path: Path) -> None:
    """EXR stores premultiplied color for partial alpha."""
    with GriptapeImage.open(encode(solid(64, 64, (100, 60, 20, 128)), "png")) as image:
        data = image.encode(ImageFormat.EXR)
    path = tmp_path / "output.exr"
    path.write_bytes(data)
    raw = oiio.ImageBuf(str(path))
    pixels = raw.get_pixels(oiio.FLOAT)
    assert isinstance(pixels, np.ndarray)
    np.testing.assert_allclose(pixels[0, 0], np.array([100, 60, 20, 255]) / 255 * 128 / 255, atol=0.002)


@pytest.mark.parametrize(
    "image_format", [ImageFormat.HDR, ImageFormat.PFM, ImageFormat.PPM, ImageFormat.PGM, ImageFormat.PBM]
)
def test_alpha_can_be_encoded_to_formats_without_alpha(image_format: ImageFormat) -> None:
    """Advertised opaque formats accept alpha sources."""
    with GriptapeImage.open(encode(solid(64, 64, (100, 60, 20, 128)), "png")) as image:
        data = image.encode(image_format)
    with GriptapeImage.open(data) as result:
        assert result.channels in (1, 3)


def test_oversized_jpeg_exif_is_rejected() -> None:
    """A large workflow cannot silently remove JPEG metadata."""
    with pytest.raises(ValueError, match="JPEG"):
        ExifMetadataDriver().inject_metadata(encode(solid(64, 64, RED), "jpg"), {"workflow": "x" * 70000})


@pytest.mark.parametrize("extension", ["png", "tif"])
def test_truncated_cached_source_raises(extension: str, tmp_path: Path) -> None:
    """Lazy tile failures prevent publishing incomplete previews."""
    data = encode(noise(512, 512), extension)
    path = tmp_path / f"broken.{extension}"
    path.write_bytes(data[: len(data) // 2])
    with GriptapeImage.open(path) as image, pytest.raises(ValueError, match="Failed"):
        GriptapeImagePipeline(image).thumbnail(64, 64).end()


def test_writer_close_failure_prevents_output_read(tmp_path: Path) -> None:
    """Finalization failure prevents returning encoded bytes."""
    writer = MagicMock()
    writer.open.return_value = True
    writer.close.return_value = False
    writer.geterror.side_effect = ["", "", "codec close failure"]
    buffer = MagicMock()
    buffer.write.return_value = True
    buffer.geterror.return_value = ""
    buffer.has_error = False
    with (
        patch("griptape_nodes.image.image_io.oiio.ImageOutput.create", return_value=writer),
        pytest.raises(ValueError, match="close failure"),
    ):
        write_file(buffer, oiio.ImageSpec(64, 64, 3, oiio.UINT8), tmp_path / "image.jp2")
    writer.close.assert_called_once()


def test_uncoded_user_comment_keeps_first_eight_letters() -> None:
    """Uncoded text is preserved in full."""
    exif = Exif.from_bytes(
        b"II\x2a\x00\x08\x00\x00\x00"
        b"\x01\x00"
        b"\x69\x87\x04\x00\x01\x00\x00\x00\x1a\x00\x00\x00"
        + bytes(4)
        + b"\x01\x00"
        + b"\x86\x92\x07\x00\x16\x00\x00\x00\x2c\x00\x00\x00"
        + bytes(4)
        + b"Screenshot of workflow"
    )
    assert exif[0x9286] == "Screenshot of workflow"
