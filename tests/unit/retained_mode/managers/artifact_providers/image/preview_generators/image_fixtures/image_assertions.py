"""Inspect encoded files against explicit expectations without production helpers."""

from pathlib import Path

import pytest

try:
    import OpenImageIO as oiio
except ModuleNotFoundError as error:
    if error.name != "OpenImageIO":
        raise
    pytest.skip("OpenImageIO fixture tests require the optional dependency", allow_module_level=True)
from PIL import Image, JpegImagePlugin

from tests.unit.retained_mode.managers.artifact_providers.image.preview_generators.image_fixtures.image_specifications import (
    EncodingChecks,
    Expectations,
    MetadataChecks,
    PixelCheck,
)


def assert_image(path: Path, expected: Expectations, *, label: str) -> None:
    """Check storage, metadata, encoding and explicit pixel regions."""
    config = oiio.ImageSpec()
    config.attribute("oiio:UnassociatedAlpha", 1)
    image = oiio.ImageBuf(str(path), 0, 0, config)
    succeeded = image.read(force=True)
    assert succeeded, f"{label}: cannot read {path}: {image.geterror()}"
    _assert_storage(path, image.nativespec(), expected, label)
    _assert_metadata(image.nativespec(), expected.metadata, label)
    if expected.encoding is not None:
        _assert_encoding(path, expected.encoding, label)
    for check in expected.pixels.checks if expected.pixels is not None else []:
        _assert_pixels(image, check, label)


def _assert_storage(path: Path, native: oiio.ImageSpec, expected: Expectations, label: str) -> None:
    if expected.dimensions is not None:
        dimensions = (native.width, native.height)
        assert dimensions == (expected.dimensions.width, expected.dimensions.height), (
            f"{label}: dimensions {dimensions}"
        )
    if expected.data_window is not None:
        window = expected.data_window
        actual = (native.x, native.y, native.width, native.height)
        assert actual == (window.x, window.y, window.width, window.height), f"{label}: data_window {actual}"
    if expected.display_window is not None:
        window = expected.display_window
        actual = (native.full_x, native.full_y, native.full_width, native.full_height)
        assert actual == (window.x, window.y, window.width, window.height), f"{label}: display_window {actual}"
    if expected.sample_type == "uint1":
        with Image.open(path) as stored:
            mode = stored.mode
        assert mode == "1", f"{label}: expected one-bit storage, actual {mode}"
    elif expected.sample_type is not None:
        name = {"float16": "half", "float32": "float"}.get(expected.sample_type, expected.sample_type)
        assert str(native.format) == name, f"{label}: expected depth {name}, actual {native.format}"
    if expected.channels is not None:
        channels = list(native.channelnames)
        # OIIO commonly decodes the grayscale colour channel as R.
        if expected.channels[0] == "Y" and channels and channels[0] == "R":
            channels[0] = "Y"
        assert channels == expected.channels, f"{label}: expected channels {expected.channels}, actual {channels}"


def _assert_metadata(native: oiio.ImageSpec, expected: MetadataChecks, label: str) -> None:
    metadata = {attribute.name: attribute.value for attribute in native.extra_attribs}
    for name, value in expected.equals.items():
        actual = metadata.get(name)
        message = f"{label}: metadata {name}: expected {value!r}, actual {actual!r}"
        assert type(actual) is type(value), message
        assert actual == value, message
    for name in expected.present:
        assert name in metadata, f"{label}: metadata {name} missing"
    for name in expected.absent:
        assert name not in metadata, f"{label}: metadata {name} unexpectedly present"


def _assert_encoding(path: Path, expected: EncodingChecks, label: str) -> None:
    with Image.open(path) as encoded:
        assert isinstance(encoded, JpegImagePlugin.JpegImageFile), f"{label}: encoding checks require JPEG"
        quantization = encoded.quantization
        sampling = JpegImagePlugin.get_sampling(encoded)
        progressive = bool(encoded.info.get("progressive", False))
    if expected.quantization is not None:
        assert quantization == expected.quantization, f"{label}: quantization {quantization}"
    if expected.sampling is not None:
        assert sampling == expected.sampling, f"{label}: sampling {sampling}"
    if expected.progressive is not None:
        assert progressive == expected.progressive, f"{label}: progressive {progressive}"


def _assert_pixels(image: oiio.ImageBuf, check: PixelCheck, label: str) -> None:
    native = image.nativespec()
    x, y, width, height = 0, 0, native.width, native.height
    if check.kind == "sample":
        x, y, width, height = check.position.x, check.position.y, 1, 1
    if check.kind == "region":
        x, y, width, height = check.region.x, check.region.y, check.region.width, check.region.height
    assert x + width <= native.width, f"{label}: pixel check outside image width"
    assert y + height <= native.height, f"{label}: pixel check outside image height"
    assert len(check.colour) == native.nchannels, f"{label}: pixel check channel count"
    native_x, native_y = x + native.x, y + native.y
    roi = oiio.ROI(native_x, native_x + width, native_y, native_y + height, 0, 1, 0, native.nchannels)
    # Compare in native code, then locate a failing pixel only when needed.
    stats = oiio.ImageBufAlgo.computePixelStats(image, roi)
    assert not any(stats.nancount), f"{label}: nonfinite NaN pixels"
    assert not any(stats.infcount), f"{label}: nonfinite infinite pixels"
    failures = [
        index
        for index, value in enumerate(check.colour)
        if abs(stats.min[index] - value) > check.max_absolute_error
        or abs(stats.max[index] - value) > check.max_absolute_error
    ]
    if failures:
        _report_pixel_failure(image, check, roi, label)


def _report_pixel_failure(image: oiio.ImageBuf, check: PixelCheck, roi: oiio.ROI, label: str) -> None:
    for row in range(roi.ybegin, roi.yend):
        for column in range(roi.xbegin, roi.xend):
            actual = image.getpixel(column, row)
            if any(abs(actual[index] - value) > check.max_absolute_error for index, value in enumerate(check.colour)):
                message = (
                    f"{label}: pixel ({column - image.nativespec().x}, {row - image.nativespec().y}): "
                    f"expected {check.colour}, actual {actual}, "
                    f"tolerance {check.max_absolute_error}"
                )
                raise AssertionError(message)
