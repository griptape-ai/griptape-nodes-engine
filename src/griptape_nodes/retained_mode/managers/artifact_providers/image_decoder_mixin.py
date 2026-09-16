"""Decode interface for the ImageArtifact family."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

    from griptape_nodes.retained_mode.managers.artifact_providers.image_situation import (
        ImageArtifactSituation,
    )


@dataclass(frozen=True)
class DecodedImageArtifact:
    """A decoded image, tagged with enough colour information to be transformed later.

    Colour management (``transform()``, needs ``source_color_space``, ``bit_depth``, and
    ``channel_layout`` to know what it's converting from - flattening this to
    a bare array here would throw that information away before anything downstream
    gets a chance to use it.

    pixel_data: Decoded pixels as a numpy array. Shape and dtype are decoder-defined
        (e.g. an 8-bit RGB decode vs. a 32-bit-float multi-channel EXR decode look
        different); ``channel_layout``/``bit_depth`` describe what's actually there.
    source_color_space: The colour space the pixel data is already in (e.g. "sRGB",
        "ACEScg", "Linear"), as reported by the source file/format. Not converted -
        that's colour management's job, done centrally by ArtifactManager after decode.
    bit_depth: Bits per channel in ``pixel_data` (e.g. 8, 16, 32).
    channel_layout: The channel composition and order (e.g. "RGB", "RGBA", "Grayscale").
    """

    pixel_data: np.ndarray
    source_color_space: str
    bit_depth: int
    channel_layout: str


class ImageArtifactDecoderMixin(ABC):
    """Opt-in mixin for providers that can decode ImageArtifact pixels.

    Not a required method on ``BaseArtifactProvider`` itself: not every provider
    has pixels to decode (a future ``DocumentArtifact`` provider, for instance).
    A provider opts in via multiple inheritance, mirroring how ``UIOptionsMixin``
    (``exe_types/core_types.py``) is opted into by ``Parameter``/``ParameterGroup``.
    Unlike ``UIOptionsMixin``, which only offers concrete helpers, this mixin
    declares ``decode()`` as abstract: once a provider opts in, it must actually
    implement decoding rather than silently inheriting a no-op.
    """

    @abstractmethod
    def decode(self, source_path: str, situation: ImageArtifactSituation) -> DecodedImageArtifact:
        """Decode the image at ``source_path`` for the given situation.

        ``situation`` lets a provider specialise its strategy for cost reasons
        (e.g. header-scan-and-downsample for ``THUMBNAIL``, full decode for
        ``VIEWER``) but never lets it opt out of a situation. ``ORIGINAL`` never
        reaches this method - ``ArtifactManager`` skips decode entirely for it.

        Args:
            source_path: Absolute path to the source file.
            situation: The context this decode is being requested for.

        Returns:
            The decoded pixels, tagged with source colour space, bit depth, and
            channel layout.
        """
        ...
