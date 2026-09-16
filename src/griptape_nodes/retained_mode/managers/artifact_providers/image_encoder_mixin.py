"""Encode interface for the ImageArtifact family."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.managers.artifact_providers.image_decoder_mixin import (
        DecodedImageArtifact,
    )
    from griptape_nodes.retained_mode.managers.artifact_providers.image_situation import (
        ImageArtifactSituation,
    )


class ImageArtifactEncoderMixin(ABC):
    """Opt-in mixin for providers that can encode a ``DecodedImageArtifact`` back to bytes.

    Not a required method on ``BaseArtifactProvider`` itself, mirroring
    ``ImageArtifactDecoderMixin``: a provider opts in via multiple inheritance and
    ``encode()`` is abstract, so opting in without implementing it still can't be
    instantiated. ``encode()`` is ``decode()``'s inverse for callers (e.g.
    ``LoadImage``) that need displayable raster bytes back out - it never
    round-trips to the original source file/format.
    """

    @abstractmethod
    def encode(self, decoded_artifact: DecodedImageArtifact, situation: ImageArtifactSituation) -> bytes:
        """Encode ``decoded_artifact`` to displayable raster bytes for the given situation.

        ``situation`` drives format/strategy internally (mirroring ``decode()``'s
        use of ``situation`` for e.g. ``THUMBNAIL`` downsampling) rather than
        taking a separate format parameter - this keeps the two mixins symmetric
        and keeps format-selection logic in the provider, not pushed onto every
        caller. ``ORIGINAL`` never reaches this method - ``ArtifactManager`` skips
        encode entirely for that situation.

        Args:
            decoded_artifact: Pixels plus colour metadata, as returned by ``decode()``.
            situation: The context this encode is being requested for.

        Returns:
            Encoded image bytes (e.g. webp/png) ready to hand back to a caller.
        """
        ...
