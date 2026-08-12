import pytest
from pydantic_ai.settings import ModelSettings

from griptape_nodes.drivers.cloud_models import (
    DEPRECATED_MODELS,
    IMAGE_DEPRECATED_MODELS,
    IMAGE_MODEL_CHOICES,
    MODEL_CHOICES,
    MODEL_CHOICES_ARGS,
    MODEL_SETTINGS,
    O_SERIES_MODELS,
    VISION_MODEL_CHOICES,
    model_settings_for,
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


class TestModelSettings:
    def test_claude_models_carry_the_catalog_max_tokens(self) -> None:
        """Regression: the sidebar sent no max_tokens, so a 4096 provider default applied."""
        for model in ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"):
            assert MODEL_SETTINGS[model]["max_tokens"] == 64000  # noqa: PLR2004

    def test_settings_are_derived_from_the_presets(self) -> None:
        """MODEL_SETTINGS must not drift from MODEL_CHOICES_ARGS."""
        for model in MODEL_CHOICES_ARGS:
            name = str(model["name"])
            preset_max_tokens = dict(model["args"]).get("max_tokens")  # type: ignore[call-overload]
            if preset_max_tokens is None:
                assert "max_tokens" not in MODEL_SETTINGS.get(name, {})
            else:
                assert MODEL_SETTINGS[name]["max_tokens"] == preset_max_tokens

    def test_driver_only_keys_are_excluded(self) -> None:
        """`stream` / `structured_output_strategy` are not ModelSettings fields."""
        for settings in MODEL_SETTINGS.values():
            assert "stream" not in settings
            assert "structured_output_strategy" not in settings

    def test_none_valued_preset_keys_are_dropped(self) -> None:
        """The o-series `top_p: None` means "omit", not "send null"."""
        assert "top_p" not in MODEL_SETTINGS.get("deepseek.r1-v1", {})

    def test_every_settings_key_is_known_to_model_settings(self) -> None:
        """Guard against a new preset key silently reaching the wire as garbage."""
        for settings in MODEL_SETTINGS.values():
            assert set(settings) <= set(ModelSettings.__annotations__)

    def test_models_without_settings_are_absent(self) -> None:
        """A lookup miss and "nothing to apply" are the same case."""
        assert model_settings_for("gpt-4o") is None
        assert model_settings_for("not-a-real-model") is None

    def test_lookup_returns_a_copy(self) -> None:
        """Callers must not be able to mutate the shared catalog."""
        settings = model_settings_for("claude-opus-5")
        assert settings is not None
        settings["max_tokens"] = 1
        assert MODEL_SETTINGS["claude-opus-5"]["max_tokens"] == 64000  # noqa: PLR2004
