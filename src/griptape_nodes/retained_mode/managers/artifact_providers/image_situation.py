"""Situation enum for the ImageArtifact family."""

from __future__ import annotations

from enum import StrEnum


class ImageArtifactSituation(StrEnum):
    """A named context an ``ImageArtifact`` (or ``ImageUrlArtifact``) is being retrieved for.

    Scoped to the ``ImageArtifact`` family only. This set is closed and
    non-negotiable per-provider: no provider can opt out of a situation, and
    no provider-extensible situations are added by this design. A future
    ``VideoArtifact``/``AudioArtifact``/``DocumentArtifact`` family defines its
    own, independent, closed situation set rather than extending this one.

    VIEWER: colour-managed raster, suitable for canvas display. The
        default/initial load for ``LoadImage``.
    THUMBNAIL: small colour-managed raster.
    ORIGINAL: the raw bytes/path, untouched. Skips decode entirely, but still
        runs a provider's existing read-vetting hooks.
    """

    VIEWER = "viewer"
    THUMBNAIL = "thumbnail"
    ORIGINAL = "original"


IMAGE_ARTIFACT_SITUATION_FALLBACKS: dict[ImageArtifactSituation, ImageArtifactSituation | None] = {
    ImageArtifactSituation.VIEWER: None,
    ImageArtifactSituation.THUMBNAIL: ImageArtifactSituation.VIEWER,
    ImageArtifactSituation.ORIGINAL: None,
}
