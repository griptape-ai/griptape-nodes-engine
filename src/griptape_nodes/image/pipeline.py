"""Transforms queued against a GriptapeImage and run together."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Self

from griptape_nodes.image.exif import TRANSPOSING_ORIENTATIONS

if TYPE_CHECKING:
    from types import TracebackType

    from griptape_nodes.image.griptape_image import GriptapeImage

# Bounds ``GriptapeImagePipeline.thumbnail`` shrinks to by default; the preview generators' default size
DEFAULT_THUMBNAIL_SIZE = 1024

_UPRIGHT = 1


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


PipelineStep = Orient | Thumbnail | ToRGB | ToRGBA | RoundCorners | Flatten

# Steps an orientation can be moved past: they don't depend on which way the pixels face
_ORIENTATION_INDEPENDENT = (ToRGB, ToRGBA)


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
