import pytest

from griptape_nodes.drivers.cloud_models import (
    DEPRECATED_MODELS,
    IMAGE_DEPRECATED_MODELS,
    IMAGE_MODEL_CHOICES,
    MODEL_CHOICES,
    O_SERIES_MODELS,
    VISION_MODEL_CHOICES,
)


class TestDeprecatedModels:
    @pytest.mark.parametrize(("deprecated", "replacement"), sorted(DEPRECATED_MODELS.items()))
    def test_replacement_is_a_live_model(self, deprecated: str, replacement: str) -> None:
        assert replacement in MODEL_CHOICES, (
            f"'{deprecated}' points at '{replacement}', which is no longer in MODEL_CHOICES."
        )

    @pytest.mark.parametrize("deprecated", sorted(DEPRECATED_MODELS))
    def test_deprecated_model_is_not_also_live(self, deprecated: str) -> None:
        assert deprecated not in MODEL_CHOICES


class TestImageDeprecatedModels:
    @pytest.mark.parametrize(("deprecated", "replacement"), sorted(IMAGE_DEPRECATED_MODELS.items()))
    def test_replacement_is_a_live_image_model(self, deprecated: str, replacement: str) -> None:
        assert replacement in IMAGE_MODEL_CHOICES, (
            f"'{deprecated}' points at '{replacement}', which is no longer in IMAGE_MODEL_CHOICES."
        )

    @pytest.mark.parametrize("deprecated", sorted(IMAGE_DEPRECATED_MODELS))
    def test_deprecated_image_model_is_not_also_live(self, deprecated: str) -> None:
        assert deprecated not in IMAGE_MODEL_CHOICES


class TestModelChoices:
    def test_vision_choices_are_a_subset_of_all_choices(self) -> None:
        assert set(VISION_MODEL_CHOICES) <= set(MODEL_CHOICES)

    def test_o_series_models_are_live(self) -> None:
        assert set(MODEL_CHOICES) >= O_SERIES_MODELS
