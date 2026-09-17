"""Tests for the ArtifactFamily protocol and the ImageFamily concrete family (GH#4741 parent).

Pure refactor step: ImageFamily bundles pieces that already exist elsewhere
(ImageArtifactSituation, IMAGE_ARTIFACT_SITUATION_FALLBACKS, the decoder/encoder
mixins) so FamilyRegistry can look them up by family instead of hardcoding
"Image". Nothing else imports ArtifactFamily/ImageFamily yet.
"""

from griptape_nodes.retained_mode.managers.artifact_providers.artifact_family import (
    ArtifactFamily,
    ImageFamily,
)
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


class TestImageFamily:
    def test_image_family_exposes_correct_situation_type(self) -> None:
        assert ImageFamily.situation_type is ImageArtifactSituation

    def test_image_family_exposes_correct_fallbacks(self) -> None:
        assert ImageFamily.situation_fallbacks is IMAGE_ARTIFACT_SITUATION_FALLBACKS

    def test_image_family_exposes_correct_mixins(self) -> None:
        assert ImageFamily.decoder_mixin is ImageArtifactDecoderMixin
        assert ImageFamily.encoder_mixin is ImageArtifactEncoderMixin

    def test_image_family_exposes_family_id(self) -> None:
        assert ImageFamily.family_id == "image"

    def test_image_family_satisfies_protocol(self) -> None:
        assert isinstance(ImageFamily, ArtifactFamily)


class TestArtifactFamilyProtocol:
    def test_class_missing_required_attributes_does_not_satisfy_protocol(self) -> None:
        class _NotAFamily:
            pass

        assert not isinstance(_NotAFamily, ArtifactFamily)
