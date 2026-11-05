"""Transforms queued against a GriptapeImage and run together."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Self

from griptape_nodes.image.exif import TRANSPOSING_ORIENTATIONS

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

    import numpy as np

    from griptape_nodes.image.griptape_image import GriptapeImage

# Bounds ``GriptapeImagePipeline.thumbnail`` shrinks to by default; the preview generators' default size
DEFAULT_THUMBNAIL_SIZE = 1024

_UPRIGHT = 1

# An 8-bit color: gray, gray and alpha, RGB, or RGBA, each 0-255
Color = tuple[int, ...]


class ResizeFilter(StrEnum):
    """How ``resize`` and ``rotate`` sample the source, named as Pillow names its filters."""

    NEAREST = "nearest"
    BOX = "box"
    BILINEAR = "bilinear"
    HAMMING = "hamming"
    BICUBIC = "bicubic"
    LANCZOS = "lanczos"


class TextAlign(StrEnum):
    """How ``draw_text`` lines up the lines of multiline text."""

    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"


@dataclass(frozen=True)
class Orient:
    """Rotate and flip as an EXIF orientation value (2-8) says to display the image."""

    orientation: int


@dataclass(frozen=True)
class Thumbnail:
    """Shrink to fit within the bounds, keeping the aspect ratio. Never enlarges."""

    max_width: int
    max_height: int


@dataclass(frozen=True)
class ToRGB:
    """Convert to three-channel color, discarding any alpha."""


@dataclass(frozen=True)
class ToRGBA:
    """Convert to four-channel color, adding an opaque alpha if there was none."""


@dataclass(frozen=True)
class RoundCorners:
    """Convert to RGBA and make the alpha transparent outside a rectangle with rounded corners.

    The radius is ``radius_ratio`` of the shorter side, at most half of it.
    """

    radius_ratio: float

    def radius_pixels(self, width: int, height: int) -> int:
        shorter_side = min(width, height)
        return min(int(self.radius_ratio * shorter_side), shorter_side // 2)


@dataclass(frozen=True)
class Flatten:
    """Composite over an opaque 8-bit RGB ``background`` and drop alpha."""

    background: tuple[int, int, int]


@dataclass(frozen=True)
class Resize:
    """Scale to exactly ``width`` x ``height``, whatever the aspect ratio."""

    width: int
    height: int
    resize_filter: ResizeFilter = ResizeFilter.LANCZOS


@dataclass(frozen=True)
class Crop:
    """Keep the box from (left, top) up to, not including, (right, bottom).

    Parts of the box outside the image come out zero: black, and transparent if the image has alpha.
    """

    left: int
    top: int
    right: int
    bottom: int


@dataclass(frozen=True)
class Rotate:
    """Rotate counterclockwise by ``degrees`` about ``center`` (the image center when None).

    ``expand`` grows the canvas to hold the whole rotated image; otherwise it keeps its size.
    Uncovered corners take ``fill``; with no fill they are transparent if the image has alpha and
    black if it doesn't. Multiples of 90 degrees about the center with ``expand`` move pixels
    without resampling.
    """

    degrees: float
    expand: bool = False
    fill: Color | None = None
    center: tuple[float, float] | None = None
    resize_filter: ResizeFilter = ResizeFilter.BICUBIC


@dataclass(frozen=True)
class FlipHorizontal:
    """Mirror left to right."""


@dataclass(frozen=True)
class FlipVertical:
    """Mirror top to bottom."""


@dataclass(frozen=True)
class Pad:
    """Grow the canvas by the given number of pixels on each side, filled with ``fill``.

    A fill with partial alpha adds an alpha channel to an image without one.
    """

    left: int
    top: int
    right: int
    bottom: int
    fill: Color = (0, 0, 0, 0)


@dataclass(frozen=True)
class ToGray:
    """Convert to one-channel gray (ITU-R 601 luma, as Pillow's "L" mode), discarding any alpha."""


@dataclass(frozen=True)
class ToUint8:
    """Convert to 8 bits per channel. Float pixels get the sRGB curve first, as they do when encoded for display."""


@dataclass(frozen=True)
class Brightness:
    """Scale color by ``factor``: 0 is black, 1 is unchanged. Alpha is unchanged. As Pillow's ``ImageEnhance.Brightness``."""

    factor: float


@dataclass(frozen=True)
class Contrast:
    """Scale color away from the image's mean gray by ``factor``: 0 is flat gray, 1 is unchanged.

    Alpha is unchanged. As Pillow's ``ImageEnhance.Contrast``.
    """

    factor: float


@dataclass(frozen=True)
class Saturation:
    """Scale color away from each pixel's gray by ``factor``: 0 is gray, 1 is unchanged.

    Alpha is unchanged. As Pillow's ``ImageEnhance.Color``.
    """

    factor: float


@dataclass(frozen=True)
class Invert:
    """Invert color, leaving alpha as it is."""


@dataclass(frozen=True)
class GaussianBlur:
    """Blur with a Gaussian of standard deviation ``radius`` pixels, as Pillow's ``ImageFilter.GaussianBlur``.

    Edges extend outward rather than darkening, and color under transparent pixels doesn't bleed.
    """

    radius: float


@dataclass(frozen=True)
class Dilate:
    """Replace each channel value with the largest in a ``size`` x ``size`` square around it."""

    size: int


@dataclass(frozen=True)
class Erode:
    """Replace each channel value with the smallest in a ``size`` x ``size`` square around it."""

    size: int


@dataclass(frozen=True)
class FindEdges:
    """Highlight edges in color with Pillow's ``ImageFilter.FIND_EDGES`` kernel, leaving alpha as it is."""


@dataclass(frozen=True, eq=False)
class Composite:
    """Draw ``overlay`` over the image with its top-left corner at (x, y), its alpha scaled by ``opacity``.

    Parts of the overlay outside the image are cut off. The result keeps the image's size and
    pixel type, and its alpha channel if it has one; a color overlay on a gray image makes it color.
    The image's metadata is kept; the overlay's is not.
    """

    overlay: GriptapeImage
    x: int = 0
    y: int = 0
    opacity: float = 1.0


@dataclass(frozen=True, eq=False)
class SetAlpha:
    """Replace the alpha channel, adding one if there was none.

    ``alpha`` is a constant 0-1 or an image the same size whose gray value (luma for color) becomes the alpha.
    """

    alpha: GriptapeImage | float


@dataclass(frozen=True)
class DrawText:
    """Draw ``text`` with its top-left corner at (x, y), in the bundled sans-serif font at ``size`` pixels.

    Lines of multiline text are ``spacing`` pixels apart and lined up by ``align``. Text over the
    edges is cut off.
    """

    text: str
    x: int
    y: int
    size: int
    color: Color = (255, 255, 255)
    align: TextAlign = TextAlign.LEFT
    spacing: int = 4


@dataclass(frozen=True)
class DrawRectangle:
    """Draw the box from (left, top) up to, not including, (right, bottom).

    ``fill`` colors the inside and ``outline`` a border ``width`` pixels thick inside the box's edge.
    Either can be None to leave it out.
    """

    left: int
    top: int
    right: int
    bottom: int
    fill: Color | None = None
    outline: Color | None = None
    width: int = 1


@dataclass(frozen=True)
class DrawLine:
    """Draw a line ``width`` pixels thick from (x0, y0) to (x1, y1), both ends included."""

    x0: float
    y0: float
    x1: float
    y1: float
    color: Color = (255, 255, 255)
    width: int = 1


@dataclass(frozen=True, eq=False)
class Apply:
    """Replace the pixels with ``function``'s result.

    ``function`` gets a height x width x channels NumPy array of the pixels in their own type
    (uint8 0-255, uint16 0-65535, float 0-1), with unassociated alpha last, and returns a new one
    of any supported type with 1-4 channels. The metadata is kept, as for other steps.
    """

    function: Callable[[np.ndarray], np.ndarray] = field(repr=False)


PipelineStep = (
    Orient
    | Thumbnail
    | ToRGB
    | ToRGBA
    | RoundCorners
    | Flatten
    | Resize
    | Crop
    | Rotate
    | FlipHorizontal
    | FlipVertical
    | Pad
    | ToGray
    | ToUint8
    | Brightness
    | Contrast
    | Saturation
    | Invert
    | GaussianBlur
    | Dilate
    | Erode
    | FindEdges
    | Composite
    | SetAlpha
    | DrawText
    | DrawRectangle
    | DrawLine
    | Apply
)

# Steps an orientation can be moved past: they work pixel by pixel, whichever way the pixels face
_ORIENTATION_INDEPENDENT = (ToRGB, ToRGBA, ToGray, ToUint8, Brightness, Contrast, Saturation, Invert)


class GriptapeImagePipeline:
    """Transforms queued against an image, run by ``end``.

    Steps run in the order queued, except that an EXIF transpose ahead of a ``thumbnail`` runs
    after it, on the smaller image, with the bounds swapped for a 90 degree turn. The output is
    the same; the rotation costs a fraction as much.

        with GriptapeImage.open(data) as source:
            pipeline = GriptapeImagePipeline(source)
            pipeline.exif_transpose().thumbnail(512, 512).round_corners(0.02)
            with pipeline.end() as preview:
                encoded = preview.encode(ImageFormat.WEBP)

    The source image is left as it was; ``end`` returns a new image for the caller to close. It
    carries the source's metadata, updated to describe the new pixels: an EXIF transpose resets
    the Orientation tag, and steps that resize update the EXIF pixel dimensions.
    """

    def __init__(self, image: GriptapeImage) -> None:
        self._image = image
        self._steps: list[PipelineStep | _ExifTranspose] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._steps.clear()

    def exif_transpose(self) -> Self:
        """Rotate and flip the image as its EXIF orientation tag says to display it."""
        self._steps.append(_ExifTranspose())
        return self

    def thumbnail(self, max_width: int = DEFAULT_THUMBNAIL_SIZE, max_height: int = DEFAULT_THUMBNAIL_SIZE) -> Self:
        """Shrink to fit within the bounds, keeping the aspect ratio. Never enlarges."""
        self._steps.append(Thumbnail(max_width, max_height))
        return self

    def to_rgb(self) -> Self:
        """Convert to three-channel color, discarding any alpha."""
        self._steps.append(ToRGB())
        return self

    def to_rgba(self) -> Self:
        """Convert to four-channel color, adding an opaque alpha if there was none."""
        self._steps.append(ToRGBA())
        return self

    def round_corners(self, radius_ratio: float) -> Self:
        """Make the corners transparent with a radius of ``radius_ratio`` of the shorter side (at most half)."""
        self._steps.append(RoundCorners(radius_ratio))
        return self

    def flatten(self, background: tuple[int, int, int] = (255, 255, 255)) -> Self:
        """Composite over an opaque 8-bit RGB background and drop alpha."""
        self._steps.append(Flatten(background))
        return self

    def resize(self, width: int, height: int, resize_filter: ResizeFilter = ResizeFilter.LANCZOS) -> Self:
        """Scale to exactly ``width`` x ``height``, whatever the aspect ratio."""
        self._steps.append(Resize(width, height, resize_filter))
        return self

    def crop(self, left: int, top: int, right: int, bottom: int) -> Self:
        """Keep the box from (left, top) up to, not including, (right, bottom). Parts outside the image come out zero."""
        self._steps.append(Crop(left, top, right, bottom))
        return self

    def rotate(
        self,
        degrees: float,
        *,
        expand: bool = False,
        fill: Color | None = None,
        center: tuple[float, float] | None = None,
        resize_filter: ResizeFilter = ResizeFilter.BICUBIC,
    ) -> Self:
        """Rotate counterclockwise by ``degrees``. See ``Rotate``."""
        self._steps.append(Rotate(degrees, expand, fill, center, resize_filter))
        return self

    def flip_horizontal(self) -> Self:
        """Mirror left to right."""
        self._steps.append(FlipHorizontal())
        return self

    def flip_vertical(self) -> Self:
        """Mirror top to bottom."""
        self._steps.append(FlipVertical())
        return self

    def pad(self, left: int, top: int, right: int, bottom: int, fill: Color = (0, 0, 0, 0)) -> Self:
        """Grow the canvas by the given number of pixels on each side, filled with ``fill``."""
        self._steps.append(Pad(left, top, right, bottom, fill))
        return self

    def to_gray(self) -> Self:
        """Convert to one-channel gray, discarding any alpha."""
        self._steps.append(ToGray())
        return self

    def to_uint8(self) -> Self:
        """Convert to 8 bits per channel."""
        self._steps.append(ToUint8())
        return self

    def brightness(self, factor: float) -> Self:
        """Scale color by ``factor``: 0 is black, 1 is unchanged."""
        self._steps.append(Brightness(factor))
        return self

    def contrast(self, factor: float) -> Self:
        """Scale color away from the mean gray by ``factor``: 0 is flat gray, 1 is unchanged."""
        self._steps.append(Contrast(factor))
        return self

    def saturation(self, factor: float) -> Self:
        """Scale color away from each pixel's gray by ``factor``: 0 is gray, 1 is unchanged."""
        self._steps.append(Saturation(factor))
        return self

    def invert(self) -> Self:
        """Invert color, leaving alpha as it is."""
        self._steps.append(Invert())
        return self

    def gaussian_blur(self, radius: float) -> Self:
        """Blur with a Gaussian of standard deviation ``radius`` pixels."""
        self._steps.append(GaussianBlur(radius))
        return self

    def dilate(self, size: int) -> Self:
        """Take each channel's largest value in a ``size`` x ``size`` square."""
        self._steps.append(Dilate(size))
        return self

    def erode(self, size: int) -> Self:
        """Take each channel's smallest value in a ``size`` x ``size`` square."""
        self._steps.append(Erode(size))
        return self

    def find_edges(self) -> Self:
        """Highlight edges with Pillow's ``FIND_EDGES`` kernel."""
        self._steps.append(FindEdges())
        return self

    def composite(self, overlay: GriptapeImage, x: int = 0, y: int = 0, opacity: float = 1.0) -> Self:
        """Draw ``overlay`` over the image at (x, y). See ``Composite``."""
        self._steps.append(Composite(overlay, x, y, opacity))
        return self

    def set_alpha(self, alpha: GriptapeImage | float) -> Self:
        """Replace the alpha with a constant 0-1 or a same-size image's gray value."""
        self._steps.append(SetAlpha(alpha))
        return self

    def draw_text(  # noqa: PLR0913
        self,
        text: str,
        x: int,
        y: int,
        *,
        size: int,
        color: Color = (255, 255, 255),
        align: TextAlign = TextAlign.LEFT,
        spacing: int = 4,
    ) -> Self:
        """Draw ``text`` with its top-left corner at (x, y). See ``DrawText``."""
        self._steps.append(DrawText(text, x, y, size, color, align, spacing))
        return self

    def draw_rectangle(  # noqa: PLR0913
        self,
        left: int,
        top: int,
        right: int,
        bottom: int,
        *,
        fill: Color | None = None,
        outline: Color | None = None,
        width: int = 1,
    ) -> Self:
        """Draw a filled and/or outlined box. See ``DrawRectangle``."""
        self._steps.append(DrawRectangle(left, top, right, bottom, fill, outline, width))
        return self

    def draw_line(  # noqa: PLR0913
        self,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        *,
        color: Color = (255, 255, 255),
        width: int = 1,
    ) -> Self:
        """Draw a line ``width`` pixels thick between two points."""
        self._steps.append(DrawLine(x0, y0, x1, y1, color, width))
        return self

    def apply(self, function: Callable[[np.ndarray], np.ndarray]) -> Self:
        """Replace the pixels with ``function``'s result on them as a NumPy array. See ``Apply``."""
        self._steps.append(Apply(function))
        return self

    def end(self) -> GriptapeImage:
        """Run the queued steps and return the resulting image. The queue is emptied."""
        steps = plan_steps(self._steps, self._orientation())
        self._steps.clear()
        return self._image._run_pipeline(steps)

    def _orientation(self) -> int:
        if not any(isinstance(step, _ExifTranspose) for step in self._steps):
            return _UPRIGHT
        exif = self._image.metadata.exif
        if exif is None:
            return _UPRIGHT
        return exif.orientation


@dataclass(frozen=True)
class _ExifTranspose:
    """Queued by ``exif_transpose``; becomes an ``Orient`` once the source's orientation is read."""


def plan_steps(steps: list[PipelineStep | _ExifTranspose], orientation: int) -> list[PipelineStep]:
    """Resolve the first EXIF transpose to ``orientation`` and move it past a following thumbnail."""
    planned: list[PipelineStep] = []
    pending: Orient | None = None
    # Only the first transpose applies: after it the pixels are upright, as an image with no
    # orientation tag would be
    resolved = False
    for step in steps:
        if isinstance(step, _ExifTranspose):
            if not resolved and orientation != _UPRIGHT:
                pending = Orient(orientation)
            resolved = True
            continue
        if pending is None:
            planned.append(step)
            continue
        if isinstance(step, _ORIENTATION_INDEPENDENT):
            planned.append(step)
            continue
        if isinstance(step, Thumbnail):
            planned.append(_bounds_before_orienting(step, pending.orientation))
            planned.append(pending)
            pending = None
            continue
        planned.append(pending)
        planned.append(step)
        pending = None
    if pending is not None:
        planned.append(pending)
    return planned


def _bounds_before_orienting(thumbnail: Thumbnail, orientation: int) -> Thumbnail:
    if orientation not in TRANSPOSING_ORIENTATIONS:
        return thumbnail
    return replace(thumbnail, max_width=thumbnail.max_height, max_height=thumbnail.max_width)
