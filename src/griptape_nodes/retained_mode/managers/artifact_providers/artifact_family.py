"""Artifact family contract: bundles the pieces that make a family independently pluggable.

An ``ArtifactFamily`` groups a situation enum, its fallback chain, and its
decoder/encoder mixins under a single ``family_id``, so ``FamilyRegistry`` can
resolve providers by family instead of a hardcoded family name (e.g. "Image").
``ImageFamily`` is the first concrete family, assembled from pieces that
already exist (``ImageArtifactSituation``, ``IMAGE_ARTIFACT_SITUATION_FALLBACKS``,
``ImageArtifactDecoderMixin``, ``ImageArtifactEncoderMixin``) rather than
duplicating them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Protocol, cast, runtime_checkable

from griptape_nodes.retained_mode.managers.artifact_providers.image_decoder_mixin import (
    ImageArtifactDecoderMixin,
)
from griptape_nodes.retained_mode.managers.artifact_providers.image_encoder_mixin import (
    ImageArtifactEncoderMixin,
)
from griptape_nodes.retained_mode.managers.artifact_providers.image_situation import (
    IMAGE_ARTIFACT_SITUATION_FALLBACKS,
    ImageArtifactSituation,
)

if TYPE_CHECKING:
    from abc import ABC
    from enum import StrEnum


@runtime_checkable
class ArtifactFamily(Protocol):
    """Structural contract a concrete family class must satisfy.

    Implemented by a plain class with ``ClassVar`` attributes, not instantiated -
    ``FamilyRegistry`` works with family classes, mirroring how
    ``BaseArtifactProvider`` subclasses are looked up by class, not instance.
    """

    family_id: ClassVar[str]
    situation_type: ClassVar[type[StrEnum]]
    situation_fallbacks: ClassVar[dict[StrEnum, StrEnum | None]]
    decoder_mixin: ClassVar[type[ABC]]
    encoder_mixin: ClassVar[type[ABC]]


class ImageFamily:
    """The ImageArtifact family: viewer/thumbnail/original situations, Pillow-backed by default."""

    family_id: ClassVar[str] = "image"
    situation_type: ClassVar[type[StrEnum]] = ImageArtifactSituation
    situation_fallbacks: ClassVar[dict[StrEnum, StrEnum | None]] = cast(
        "dict[StrEnum, StrEnum | None]", IMAGE_ARTIFACT_SITUATION_FALLBACKS
    )
    decoder_mixin: ClassVar[type[ABC]] = ImageArtifactDecoderMixin
    encoder_mixin: ClassVar[type[ABC]] = ImageArtifactEncoderMixin
