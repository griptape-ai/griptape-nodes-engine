"""OpenImageIO-based thumbnail generation."""

from __future__ import annotations

import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import OpenImageIO as oiio

from griptape_nodes.drivers.image_metadata.png_chunks import read_png_text, rewrite_png_text_bytes
from griptape_nodes.files.path_utils import canonicalize_for_io
from griptape_nodes.retained_mode.managers.artifact_providers.image._backends import openimageio

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.managers.artifact_providers.base_generator_parameters import (
        BaseGeneratorParameters,
    )
from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.thumbnail_generator_base import (
    BaseThumbnailGenerator,
)
from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.thumbnail_parameters import (
    ThumbnailParameters,
)

logger = logging.getLogger("griptape_nodes")

# OpenImageIO's built-in colour config. It is named explicitly so a studio `$OCIO` config, which
# may not define these spaces, cannot change what a thumbnail looks like or stop it being made.
COLOUR_CONFIG = "ocio://default"
SRGB_COLOUR_SPACE = "srgb_rec709_scene"
LINEAR_COLOUR_SPACE = "lin_rec709_scene"
FLOAT_PIXEL_FORMATS = (oiio.HALF, oiio.FLOAT, oiio.DOUBLE)

GRAYSCALE_ALPHA_CHANNELS = 2
GRAYSCALE_ALPHA_CHANNEL_NAMES = ("Y", "A")
RGB_CHANNEL_NAMES = ("R", "G", "B")
RGBA_CHANNEL_NAMES = (*RGB_CHANNEL_NAMES, "A")


class OpenImageIOThumbnailGenerator(BaseThumbnailGenerator):
    """Thumbnail generator with dimension constraints.

    Resizes images to fit within max_width x max_height while preserving aspect ratio.
    """

    @classmethod
    def get_friendly_name(cls) -> str:
        """Human-readable name."""
        return "Standard Thumbnail Generation"

    @classmethod
    def get_supported_source_formats(cls) -> set[str]:
        """Source formats this generator can process."""
        return openimageio.get_supported_source_formats()

    @classmethod
    def get_supported_preview_formats(cls) -> set[str]:
        """Preview formats this generator produces."""
        return {"webp", "jpg", "png"}

    @classmethod
    def get_parameters(cls) -> type[BaseGeneratorParameters]:
        """Get parameter model class."""
        return ThumbnailParameters

    def _read_image(self, source: Path, *, max_source_pixels: int) -> oiio.ImageBuf:
        """Decode the private source file eagerly before rendering."""
        config = oiio.ImageSpec()
        # Prevent OIIO from premultiplying the alpha so we write back what we read.
        config.attribute("oiio:UnassociatedAlpha", 1)

        image = oiio.ImageBuf(str(source), 0, 0, config)
        spec = image.spec()
        source_pixels = spec.width * spec.height * max(1, spec.depth)
        if source_pixels > max_source_pixels:
            msg = (
                f"Attempted to create a preview for '{self.source_file_location}'. Failed due to the image "
                f"dimensions {spec.width} x {spec.height} containing {source_pixels:,} pixels, exceeding "
                f"max_thumbnail_source_pixels={max_source_pixels:,}. Increase this setting only if your "
                "machine has enough memory."
            )
            raise OSError(msg)
        if not image.read(force=True):
            reason = image.geterror()
            msg = f"Attempted to load image '{self.source_file_location}'. Failed due to {reason}."
            raise OSError(msg)

        self._source_png_text = {}
        if self.preview_format == "png" and image.nativespec().get_string_attribute("oiio:SourceFileFormat") == "png":
            self._source_png_text = read_png_text(source)
        return image

    def _encode_thumbnail(
        self, source_bytes: bytes, temp_directory: Path, *, preserve_text: bool, max_source_pixels: int
    ) -> bytes:
        """Decode, render and remove private source/output storage within one worker operation."""
        directory = canonicalize_for_io(temp_directory)
        directory.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="gtn-thumbnail-", dir=directory) as private_directory:
            source_suffix = Path(self.source_file_location).suffix
            source = canonicalize_for_io(Path(private_directory) / f"source{source_suffix}")
            source.write_bytes(source_bytes)
            image = self._read_image(source, max_source_pixels=max_source_pixels)
            source_text = self._source_png_text
            if source_text:
                spec = image.specmod()
                rendering_attributes = {"oiio:ColorSpace", "ICCProfile", "Orientation", "oiio:UnassociatedAlpha"}
                for key in source_text:
                    if key not in rendering_attributes:
                        spec.erase_attribute(key)
            scratch = canonicalize_for_io(Path(private_directory) / f"preview.{self.preview_format}")
            self._render_thumbnail(image, scratch)
            content = scratch.read_bytes()
        if preserve_text and source_text:
            content = rewrite_png_text_bytes(content, source_text)
        return content

    def _render_thumbnail(self, image: oiio.ImageBuf, destination: Path) -> None:
        """Resize and encode a scratch file. Normal byte publication adds workflow metadata."""
        # Working-buffer conversions can change nativespec; retain the source's colour intent.
        source_space = self._source_colour_space(image)
        output_format = oiio.UINT8
        if self.preview_format == "png" and image.nativespec().format == oiio.UINT16:
            output_format = oiio.UINT16

        image = self._fit_display_window(image)

        image = oiio.ImageBufAlgo.reorient(image)
        if image.has_error:
            msg = f"Attempted to orient image '{self.source_file_location}'. Failed due to {image.geterror()}."
            raise OSError(msg)

        image = self._ensure_straight_alpha(image)
        image = self._normalize_channels(image)
        image = self._convert_to_srgb(image, source_space=source_space)

        if self.preview_format in {"jpg", "jpeg"}:
            # Match Pillow's default JPEG quality and chroma subsampling.
            image.specmod().attribute("compression", "jpeg:75")
            image.specmod().attribute("CompressionQuality", 75)
            image.specmod().attribute("jpeg:subsampling", "4:2:0")
            image.specmod().attribute("jpeg:progressive", 0)

        if self.preview_format == "webp":
            # Match Pillow's static WebP encoding defaults.
            image.specmod().attribute("compression", "webp:80")
            image.specmod().attribute("webp:method", 4)

        if not image.write(str(destination), output_format, self.preview_format):
            msg = f"Attempted to encode image '{self.source_file_location}'. Failed due to {image.geterror()}."
            raise OSError(msg)

    def _fit_display_window(self, image: oiio.ImageBuf) -> oiio.ImageBuf:
        """Fit the display frame to the bounds, clipping overscan and padding missing pixels."""
        spec = image.spec()
        max_width, max_height = self.params.max_width, self.params.max_height
        # Fit the unrotated image against swapped bounds when reorientation swaps axes.
        if openimageio.orientation_swaps_axes(spec):
            max_width, max_height = max_height, max_width
        scale = min(1, max_width / spec.full_width, max_height / spec.full_height)
        width = max(1, round(spec.full_width * scale))
        height = max(1, round(spec.full_height * scale))
        display = image.roi_full
        # Clip overscan before filtering so pixels outside the frame cannot bleed into it.
        left = max(spec.x, display.xbegin)
        top = max(spec.y, display.ybegin)
        right = min(spec.x + spec.width, display.xend)
        bottom = min(spec.y + spec.height, display.yend)
        has_overscan = (
            spec.x < display.xbegin
            or spec.y < display.ybegin
            or spec.x + spec.width > display.xend
            or spec.y + spec.height > display.yend
        )
        if has_overscan and left < right and top < bottom:
            image = self._checked(
                oiio.ImageBufAlgo.crop(image, oiio.ROI(left, right, top, bottom, 0, 1, 0, spec.nchannels)),
                "frame",
            )
        if width != spec.full_width or height != spec.full_height:
            image = self._resize_image(image, width, height)
        elif (
            spec.x != 0
            or spec.y != 0
            or spec.full_x != 0
            or spec.full_y != 0
            or spec.width != spec.full_width
            or spec.height != spec.full_height
        ):
            image = self._checked(oiio.ImageBufAlgo.cut(image, display), "frame")

        return image

    def _resize_image(self, image: oiio.ImageBuf, width: int, height: int) -> oiio.ImageBuf:
        """Filter associated colour and alpha together; the shared output boundary straightens it."""
        spec = image.spec()
        has_alpha = spec.alpha_channel >= 0
        if has_alpha and spec.format not in FLOAT_PIXEL_FORMATS:
            # Integer premultiplication discards colour precision at small alpha values.
            image = self._checked(image.copy(oiio.FLOAT), "prepare alpha precision for")
        if has_alpha and spec.get_int_attribute("oiio:UnassociatedAlpha", 0) == 1:
            image = self._checked(oiio.ImageBufAlgo.premult(image), "premultiply")
        resized = oiio.ImageBufAlgo.resize(
            image, filtername="lanczos3", roi=oiio.ROI(0, width, 0, height, 0, 1, 0, spec.nchannels)
        )
        if resized.has_error:
            msg = f"Attempted to resize image '{self.source_file_location}'. Failed due to {resized.geterror()}."
            raise OSError(msg)
        return resized

    def _ensure_straight_alpha(self, image: oiio.ImageBuf) -> oiio.ImageBuf:
        """Straighten associated colour before output channel selection can discard its alpha."""
        spec = image.spec()
        if spec.alpha_channel < 0 or spec.get_int_attribute("oiio:UnassociatedAlpha", 0) == 1:
            return image
        return self._checked(oiio.ImageBufAlgo.unpremult(image), "unpremultiply")

    def _convert_to_srgb(self, image: oiio.ImageBuf, *, source_space: str | None) -> oiio.ImageBuf:
        """Convert scene-referred pixels to sRGB, which is what the 8-bit preview formats hold.

        Scene-linear light written without a transform is too dark and clips its highlights.
        PNG, JPEG, WebP and other 8-bit sources are already display-referred, so they are
        returned as they are. A float source with no colour space is taken to be scene-linear.
        A source in a space the built-in config does not define, such as a camera log curve, is
        also returned as it is, and the preview says so in the log.
        """
        if source_space is None or source_space == SRGB_COLOUR_SPACE:
            return image

        config = oiio.ColorConfig(COLOUR_CONFIG)
        if config.getColorSpaceIndex(source_space) < 0:
            logger.warning(
                "Attempted to convert '%s' to sRGB for its preview. Its colour space '%s' is not in the built-in "
                "colour config, so the preview shows the pixel values unchanged.",
                self.source_file_location,
                source_space,
            )
            return image

        # A grayscale-alpha image goes through RGBA, because OpenImageIO cannot tell it from two
        # colour channels and would convert its alpha.
        grayscale_alpha = image.spec().nchannels == GRAYSCALE_ALPHA_CHANNELS
        if grayscale_alpha:
            image = self._checked(
                oiio.ImageBufAlgo.channels(image, (0, 0, 0, 1), RGBA_CHANNEL_NAMES), "convert colours of"
            )

        # The shared output boundary has already straightened colour before channel selection.
        converted = self._checked(
            oiio.ImageBufAlgo.colorconvert(
                image, source_space, SRGB_COLOUR_SPACE, unpremult=False, colorconfig=COLOUR_CONFIG
            ),
            "convert colours of",
        )

        if grayscale_alpha:
            converted = self._checked(
                oiio.ImageBufAlgo.channels(converted, (0, 3), GRAYSCALE_ALPHA_CHANNEL_NAMES), "convert colours of"
            )
        return converted

    def _checked(self, image: oiio.ImageBuf, action: str) -> oiio.ImageBuf:
        """Return `image`, or raise when the operation that produced it failed."""
        if image.has_error:
            msg = f"Attempted to {action} '{self.source_file_location}' for its preview. Failed due to {image.geterror()}."
            raise OSError(msg)
        return image

    def _normalize_channels(self, image: oiio.ImageBuf) -> oiio.ImageBuf:
        """Select channels supported by the preview format.

        JPEG expands grayscale to RGB and drops alpha. WebP expands grayscale
        to RGB and retains alpha when present. PNG retains grayscale channels.
        Other inputs keep their first three channels for JPEG or first four
        channels for PNG/WebP. Return the original image when no conversion is needed.
        """
        channel_count = image.spec().nchannels
        max_channels = len(RGBA_CHANNEL_NAMES)
        # Map input channel counts to source indices for each output channel.
        # Repeating index 0 fills R/G/B with grayscale; index 1 retains grayscale alpha.
        grayscale_channels: dict[int, tuple[int, ...]] = {}
        if self.preview_format in {"jpg", "jpeg"}:
            max_channels = len(RGB_CHANNEL_NAMES)
            grayscale_channels = {1: (0, 0, 0), 2: (0, 0, 0)}
        elif self.preview_format == "webp":
            grayscale_channels = {1: (0, 0, 0), 2: (0, 0, 0, 1)}

        channel_order = grayscale_channels.get(channel_count, tuple(range(min(channel_count, max_channels))))
        if channel_order == tuple(range(channel_count)):
            return image

        converted = oiio.ImageBufAlgo.channels(image, channel_order, RGBA_CHANNEL_NAMES[: len(channel_order)])
        if converted.has_error:
            msg = f"Attempted to convert image channels for '{self.source_file_location}'. Failed due to {converted.geterror()}."
            raise OSError(msg)
        return converted

    @staticmethod
    def _source_colour_space(image: oiio.ImageBuf) -> str | None:
        """The colour space to convert from, or None when the pixels need no conversion."""
        tagged = image.spec().get_string_attribute("oiio:ColorSpace", "")
        if tagged:
            return tagged
        if image.nativespec().format in FLOAT_PIXEL_FORMATS:
            return LINEAR_COLOUR_SPACE
        return None
