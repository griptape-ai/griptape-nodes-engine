"""Tests for the ImageArtifactDecoderMixin/DecodedImageArtifact interface.

This is a pure-interface issue (GH#268): no provider implements ``decode()``
yet (that's GH#269 against the built-in Pillow provider). These tests only
exercise the contract shape - that the mixin is opt-in (a plain class doesn't
get it for free), that it's still abstract until ``decode()`` is implemented,
and that ``DecodedImageArtifact`` is an immutable data holder.
"""

import numpy as np
import pytest

from griptape_nodes.retained_mode.managers.artifact_providers.image_decoder_mixin import (
    DecodedImageArtifact,
    ImageArtifactDecoderMixin,
)
from griptape_nodes.retained_mode.managers.artifact_providers.image_situation import (
    ImageArtifactSituation,
)


class _IncompleteDecoder(ImageArtifactDecoderMixin):
    """Opts into the mixin but never implements decode()."""


class _CompliantDecoder(ImageArtifactDecoderMixin):
    """A minimal concrete implementation, used only to prove the contract is satisfiable."""

    def decode(self, source_path: str, situation: ImageArtifactSituation) -> DecodedImageArtifact:  # noqa: ARG002
        return DecodedImageArtifact(
            pixel_data=np.zeros((1, 1, 3)),
            source_color_space="sRGB",
            bit_depth=8,
            channel_layout="RGB",
        )


class TestImageArtifactDecoderMixin:
    def test_mixin_is_abstract_until_decode_is_implemented(self) -> None:
        with pytest.raises(TypeError, match="abstract"):
            _IncompleteDecoder()  # type: ignore[abstract]

    def test_compliant_subclass_can_be_instantiated(self) -> None:
        decoder = _CompliantDecoder()
        result = decoder.decode("/some/path.png", ImageArtifactSituation.VIEWER)
        assert isinstance(result, DecodedImageArtifact)

    def test_opting_in_is_via_inheritance_not_automatic(self) -> None:
        assert not isinstance(object(), ImageArtifactDecoderMixin)


class TestDecodedImageArtifact:
    def test_is_frozen(self) -> None:
        decoded = DecodedImageArtifact(
            pixel_data=np.zeros((2, 2, 4)),
            source_color_space="Linear",
            bit_depth=32,
            channel_layout="RGBA",
        )
        with pytest.raises(AttributeError):
            decoded.bit_depth = 16  # type: ignore[misc]

    def test_holds_expected_fields(self) -> None:
        pixels = np.ones((4, 4, 3))
        decoded = DecodedImageArtifact(
            pixel_data=pixels,
            source_color_space="ACEScg",
            bit_depth=16,
            channel_layout="RGB",
        )
        assert decoded.pixel_data is pixels
        assert decoded.source_color_space == "ACEScg"
        assert decoded.bit_depth == 16  # noqa: PLR2004
        assert decoded.channel_layout == "RGB"
