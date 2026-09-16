"""Tests for the ImageArtifactEncoderMixin interface.

This is a pure-interface issue (GH#275): these tests only exercise the contract
shape - that the mixin is opt-in (a plain class doesn't get it for free) and
that it's still abstract until ``encode()`` is implemented. The Pillow
implementation is covered separately in ``test_image_artifact_provider_encode.py``.
"""

import numpy as np
import pytest

from griptape_nodes.retained_mode.managers.artifact_providers.image_decoder_mixin import (
    DecodedImageArtifact,
)
from griptape_nodes.retained_mode.managers.artifact_providers.image_encoder_mixin import (
    ImageArtifactEncoderMixin,
)
from griptape_nodes.retained_mode.managers.artifact_providers.image_situation import (
    ImageArtifactSituation,
)


class _IncompleteEncoder(ImageArtifactEncoderMixin):
    """Opts into the mixin but never implements encode()."""


class _CompliantEncoder(ImageArtifactEncoderMixin):
    """A minimal concrete implementation, used only to prove the contract is satisfiable."""

    def encode(self, decoded_artifact: DecodedImageArtifact, situation: ImageArtifactSituation) -> bytes:  # noqa: ARG002
        return b""


class TestImageArtifactEncoderMixin:
    def test_mixin_is_abstract_until_encode_is_implemented(self) -> None:
        with pytest.raises(TypeError, match="abstract"):
            _IncompleteEncoder()  # type: ignore[abstract]

    def test_compliant_subclass_can_be_instantiated(self) -> None:
        encoder = _CompliantEncoder()
        decoded = DecodedImageArtifact(
            pixel_data=np.zeros((1, 1, 3)),
            source_color_space="sRGB",
            bit_depth=8,
            channel_layout="RGB",
        )
        result = encoder.encode(decoded, ImageArtifactSituation.VIEWER)
        assert isinstance(result, bytes)

    def test_opting_in_is_via_inheritance_not_automatic(self) -> None:
        assert not isinstance(object(), ImageArtifactEncoderMixin)
