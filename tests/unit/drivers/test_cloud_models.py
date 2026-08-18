import pytest
from pydantic_ai.settings import ModelSettings

from griptape_nodes.drivers.cloud_models import (
    _PRESET_KEY_KINDS,
    DEPRECATED_MODELS,
    IMAGE_DEPRECATED_MODELS,
    IMAGE_MODEL_CHOICES,
    MODEL_CHOICES,
    MODEL_CHOICES_ARGS,
    MODEL_SETTINGS,
    O_SERIES_MODELS,
    VISION_MODEL_CHOICES,
    _PresetKeyKind,
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
        """`top_p: None` means "omit", not "send null". Only deepseek.r1-v1 sets it."""
        assert dict(next(m for m in MODEL_CHOICES_ARGS if m["name"] == "deepseek.r1-v1")["args"])["top_p"] is None  # type: ignore[call-overload]
        assert "top_p" not in MODEL_SETTINGS.get("deepseek.r1-v1", {})

    def test_every_settings_key_is_known_to_model_settings(self) -> None:
        """Guard against a new preset key silently reaching the wire as garbage."""
        for settings in MODEL_SETTINGS.values():
            assert set(settings) <= set(ModelSettings.__annotations__)

    def test_every_preset_key_is_classified(self) -> None:
        """The reverse of the check above: nothing may fall through unclassified.

        Forwarding is an allowlist, so a preset key that is a valid
        `ModelSettings` field but absent from `_PRESET_KEY_KINDS` is silently
        dropped instead of reaching the wire — the same bug MODEL_SETTINGS exists
        to fix. Every key in every preset must be classified one way or the other.
        """
        for model in MODEL_CHOICES_ARGS:
            unclassified = set(dict(model["args"])) - set(_PRESET_KEY_KINDS)  # type: ignore[call-overload]
            assert not unclassified, (
                f"'{model['name']}' preset carries {sorted(unclassified)}, which is absent from "
                f"_PRESET_KEY_KINDS. Add each one as FORWARDED if it is a valid ModelSettings "
                f"field that should reach the wire, or DRIVER_ONLY if it configures the driver."
            )

    def test_driver_only_keys_are_not_model_settings_fields(self) -> None:
        """A key excused as driver-only must not actually be a forwardable field."""
        driver_only = {key for key, kind in _PRESET_KEY_KINDS.items() if kind is _PresetKeyKind.DRIVER_ONLY}
        assert driver_only
        assert not driver_only & set(ModelSettings.__annotations__)

    def test_forwarded_keys_are_model_settings_fields(self) -> None:
        """The allowlist itself must stay valid, not just the keys presets happen to use."""
        forwarded = {key for key, kind in _PRESET_KEY_KINDS.items() if kind is _PresetKeyKind.FORWARDED}
        assert forwarded
        assert forwarded <= set(ModelSettings.__annotations__)

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
