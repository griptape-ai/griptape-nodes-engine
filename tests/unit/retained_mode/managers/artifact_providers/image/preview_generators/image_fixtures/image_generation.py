"""Generate immutable procedural sources in pytest temporary directories."""

import math
from pathlib import Path

import pytest

try:
    import OpenImageIO as oiio
except ModuleNotFoundError as error:
    if error.name != "OpenImageIO":
        raise
    pytest.skip("OpenImageIO fixture tests require the optional dependency", allow_module_level=True)
from PIL import Image

from tests.unit.retained_mode.managers.artifact_providers.image.preview_generators.image_fixtures.image_assertions import (
    assert_image,
)
from tests.unit.retained_mode.managers.artifact_providers.image.preview_generators.image_fixtures.image_specifications import (
    ImageSpecification,
    Pattern,
    QuadrantsPattern,
    Source,
    Variant,
)

SAMPLE_TYPES = {"uint8": oiio.UINT8, "uint16": oiio.UINT16, "float16": oiio.HALF, "float32": oiio.FLOAT}


def read_image(path: Path) -> oiio.ImageBuf:
    """Read image."""
    config = oiio.ImageSpec()
    config.attribute("oiio:UnassociatedAlpha", 1)
    image = oiio.ImageBuf(str(path), 0, 0, config)
    succeeded = image.read(force=True)
    if not succeeded:
        msg = f"Failed to read {path}: {image.geterror()}"
        raise AssertionError(msg)
    return image


def _quantize(colour: list[float], sample_type: str) -> tuple[float, ...]:
    if sample_type not in {"uint8", "uint16"}:
        return tuple(colour)
    levels = {"uint8": 255, "uint16": 65535}[sample_type]
    return tuple(math.floor(value * levels + 0.5) / levels for value in colour)


def _fill(image: oiio.ImageBuf, colour: tuple[float, ...], roi: oiio.ROI | None = None) -> None:
    if roi is None:
        native_roi = oiio.ROI.All
    else:
        spec = image.spec()
        native_roi = oiio.ROI(
            roi.xbegin + spec.x,
            roi.xend + spec.x,
            roi.ybegin + spec.y,
            roi.yend + spec.y,
            roi.zbegin,
            roi.zend,
            roi.chbegin,
            roi.chend,
        )
    succeeded = oiio.ImageBufAlgo.fill(image, colour, roi=native_roi)
    if not succeeded:
        msg = f"Failed to fill source: {image.geterror()}"
        raise AssertionError(msg)


def _axis_length(axis: str, width: int, height: int) -> int:
    if axis == "x":
        return width
    return height


def _stripe_roi(axis: str, start: int, end: int, width: int, height: int) -> oiio.ROI:
    if axis == "x":
        return oiio.ROI(start, end, 0, height)
    return oiio.ROI(0, width, start, end)


def _render_quadrants(image: oiio.ImageBuf, pattern: QuadrantsPattern, sample_type: str) -> None:
    width, height = image.spec().width, image.spec().height
    for index, colour in enumerate(pattern.colours):
        if index % 2 == 0:
            x_start, x_end = 0, width // 2
        else:
            x_start, x_end = width // 2, width
        if index < len(pattern.colours) // 2:
            y_start, y_end = 0, height // 2
        else:
            y_start, y_end = height // 2, height
        _fill(image, _quantize(colour, sample_type), oiio.ROI(x_start, x_end, y_start, y_end))


def _render_pattern(image: oiio.ImageBuf, pattern: Pattern, sample_type: str) -> None:
    width, height = image.spec().width, image.spec().height
    if pattern.pattern == "solid":
        _fill(image, _quantize(pattern.colour, sample_type))
        return
    if pattern.pattern == "gradient":
        length = _axis_length(pattern.axis, width, height)
        for index in range(length):
            fraction = index / max(1, length - 1)
            colour = [start + (end - start) * fraction for start, end in zip(pattern.start, pattern.end, strict=True)]
            roi = _stripe_roi(pattern.axis, index, index + 1, width, height)
            _fill(image, _quantize(colour, sample_type), roi)
        return
    colours = pattern.colours
    if pattern.pattern == "colour_bars":
        length = _axis_length(pattern.axis, width, height)
        for index, colour in enumerate(colours):
            start = math.ceil(index * length / len(colours))
            end = math.ceil((index + 1) * length / len(colours))
            roi = _stripe_roi(pattern.axis, start, end, width, height)
            _fill(image, _quantize(colour, sample_type), roi)
        return
    if pattern.pattern == "quadrants":
        _render_quadrants(image, pattern, sample_type)
        return
    cells = pattern.cell_dimensions
    for y in range(0, height, cells.height):
        for x in range(0, width, cells.width):
            colour = colours[(x // cells.width + y // cells.height) % 2]
            _fill(
                image,
                _quantize(colour, sample_type),
                oiio.ROI(x, min(x + cells.width, width), y, min(y + cells.height, height)),
            )


def write_variant(specification: ImageSpecification, variant: Variant, directory: Path) -> Path:
    """Write and verify a source, including explicit variant read-back expectations."""
    source = specification.source
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{specification.id}-{variant.id}.{variant.format}"
    metadata = source.metadata | variant.metadata
    if variant.sample_type == "uint1":
        if metadata or variant.encoding:
            msg = "One-bit writer does not support metadata or encoding overrides"
            raise AssertionError(msg)
        pattern = source.pixels
        assert pattern.pattern == "solid"
        colour = pattern.colour
        threshold = 0.5
        with Image.new("1", (source.dimensions.width, source.dimensions.height), colour[0] >= threshold) as image:
            image.save(path)
        with Image.open(path) as stored:
            if stored.mode != "1":
                msg = f"{path}: expected one-bit storage"
                raise AssertionError(msg)
    else:
        spec = oiio.ImageSpec(source.dimensions.width, source.dimensions.height, len(source.channels), oiio.FLOAT)
        spec.x, spec.y = source.data_origin.x, source.data_origin.y
        display = source.resolved_display_window
        spec.full_x, spec.full_y = display.x, display.y
        spec.full_width, spec.full_height = display.width, display.height
        spec.channelnames = source.channels
        if "A" in source.channels:
            spec.alpha_channel = source.channels.index("A")
            if source.alpha_association == "straight":
                spec.attribute("oiio:UnassociatedAlpha", 1)
        for name, value in (metadata | variant.encoding).items():
            spec.attribute(name, value)
        image = oiio.ImageBuf(spec)
        _render_pattern(image, source.pixels, variant.sample_type)
        succeeded = image.write(str(path), SAMPLE_TYPES[variant.sample_type])
        if not succeeded:
            msg = f"Failed to write {path}: {image.geterror()}"
            raise AssertionError(msg)
    stored = read_image(path)
    _assert_encoded_source(path, stored.nativespec(), source, variant)
    if variant.expect is not None:
        assert_image(path, variant.expect, label=f"{specification.id}/{variant.id}/source")
    return path


def _assert_encoded_source(path: Path, native: oiio.ImageSpec, source: Source, variant: Variant) -> None:
    if (native.width, native.height, native.nchannels) != (
        source.dimensions.width,
        source.dimensions.height,
        len(source.channels),
    ):
        msg = f"{path}: source dimensions or channel count changed during encoding"
        raise AssertionError(msg)
    data = source.data_window
    display = source.resolved_display_window
    if (native.x, native.y, native.width, native.height) != (data.x, data.y, data.width, data.height):
        msg = f"{path}: source data_window changed during encoding"
        raise AssertionError(msg)
    if (native.full_x, native.full_y, native.full_width, native.full_height) != (
        display.x,
        display.y,
        display.width,
        display.height,
    ):
        msg = f"{path}: source display_window changed during encoding"
        raise AssertionError(msg)
    if variant.sample_type != "uint1" and native.format != SAMPLE_TYPES[variant.sample_type]:
        msg = f"{path}: expected {variant.sample_type}, stored {native.format}"
        raise AssertionError(msg)
