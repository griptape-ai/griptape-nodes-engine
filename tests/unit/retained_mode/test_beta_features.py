import json
import platform
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from griptape_nodes.retained_mode import beta_features as beta_features_module
from griptape_nodes.retained_mode.beta_features import (
    BetaFeature,
    is_beta_enabled,
    list_beta_features,
    register_beta_feature,
)
from griptape_nodes.retained_mode.events.config_events import (
    ListBetaFeaturesRequest,
    ListBetaFeaturesResultSuccess,
)
from griptape_nodes.retained_mode.managers.config_manager import ConfigManager

MAX_DAYS_AHEAD = 180


def _today() -> date:
    return datetime.now(tz=UTC).date()


# Runs against the real registry. With no features registered, pytest reports these as skipped.
@pytest.mark.parametrize("feature", list_beta_features(), ids=lambda f: f.id)
class TestBetaFeature:
    def test_not_past_remove_by(self, feature: BetaFeature) -> None:
        assert _today() <= feature.remove_by, (
            f"Beta feature '{feature.id}' (owner {feature.owner}) passed its remove_by date {feature.remove_by}. "
            "Promote it to default, delete it, or extend remove_by with a reason in the PR."
        )

    def test_remove_by_not_too_far_out(self, feature: BetaFeature) -> None:
        latest = _today() + timedelta(days=MAX_DAYS_AHEAD)
        assert feature.remove_by <= latest, (
            f"Beta feature '{feature.id}' (owner {feature.owner}) has remove_by {feature.remove_by}, "
            f"more than {MAX_DAYS_AHEAD} days out. Pick a nearer date."
        )

    def test_has_description(self, feature: BetaFeature) -> None:
        assert feature.description.strip(), f"Beta feature '{feature.id}' (owner {feature.owner}) needs a description."


def _make_feature(feature_id: str = "sample_feature", *, default: bool = False) -> BetaFeature:
    return BetaFeature(
        id=feature_id,
        name="Sample feature",
        description="Does something experimental.",
        default=default,
        owner="@someone",
        remove_by=date(2027, 1, 31),
    )


@pytest.fixture
def empty_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap in an empty registry so tests can register features without touching the real one."""
    monkeypatch.setattr(beta_features_module, "_registry", {})


@pytest.mark.usefixtures("empty_registry")
class TestRegistry:
    def test_register_returns_the_feature(self) -> None:
        feature = _make_feature()

        assert register_beta_feature(feature) is feature
        assert list_beta_features() == [feature]

    def test_duplicate_id_is_rejected(self) -> None:
        register_beta_feature(_make_feature())

        with pytest.raises(ValueError, match="sample_feature"):
            register_beta_feature(_make_feature())

    @pytest.mark.parametrize("bad_id", ["CamelCase", "1starts_with_digit", "has-dash", "has space", ""])
    def test_non_snake_case_id_is_rejected(self, bad_id: str) -> None:
        with pytest.raises(ValidationError):
            _make_feature(bad_id)

    def test_invalid_remove_by_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BetaFeature(id="x", name="X", description="X.", owner="@someone", remove_by="2027-02-30")  # pyright: ignore[reportArgumentType]


@pytest.mark.skipif(
    platform.system() == "Windows", reason="xdg_base_dirs cannot find XDG_CONFIG_HOME on Windows on GitHub Actions"
)
class TestIsBetaEnabled:
    @staticmethod
    def _manager_with_user_config(user_config_path: Path, contents: dict) -> ConfigManager:
        user_config_path.write_text(json.dumps(contents), encoding="utf-8")
        manager = ConfigManager()
        manager.load_configs()
        return manager

    @pytest.mark.parametrize("default", [True, False])
    def test_absent_key_uses_default(self, isolate_user_config: Path, *, default: bool) -> None:
        manager = self._manager_with_user_config(isolate_user_config, {})

        assert is_beta_enabled(_make_feature(default=default), manager) is default

    @pytest.mark.parametrize("value", [True, False])
    def test_configured_value_wins(self, isolate_user_config: Path, *, value: bool) -> None:
        manager = self._manager_with_user_config(isolate_user_config, {"beta_features": {"sample_feature": value}})

        assert is_beta_enabled(_make_feature(default=not value), manager) is value

    @pytest.mark.parametrize("default", [True, False])
    @pytest.mark.parametrize("value", ["maybe", "true", 1, None])
    def test_non_boolean_value_uses_default(self, isolate_user_config: Path, value: object, *, default: bool) -> None:
        """Only a real boolean counts, matching the editor's rule.

        `get_config_value(cast_type=bool)` would read `"maybe"` and `"true"` as True, and a stored
        null as None before the default applies, which turns off a feature whose default is True.
        """
        manager = self._manager_with_user_config(isolate_user_config, {"beta_features": {"sample_feature": value}})

        assert is_beta_enabled(_make_feature(default=default), manager) is default


@pytest.mark.skipif(
    platform.system() == "Windows", reason="xdg_base_dirs cannot find XDG_CONFIG_HOME on Windows on GitHub Actions"
)
@pytest.mark.usefixtures("empty_registry")
class TestListBetaFeaturesRequest:
    def test_empty_registry_returns_empty_list(self) -> None:
        result = ConfigManager().on_handle_list_beta_features_request(ListBetaFeaturesRequest())

        assert isinstance(result, ListBetaFeaturesResultSuccess)
        assert result.features == []

    def test_returns_features_in_the_editor_contract_shape(self) -> None:
        register_beta_feature(_make_feature())

        result = ConfigManager().on_handle_list_beta_features_request(ListBetaFeaturesRequest())

        assert isinstance(result, ListBetaFeaturesResultSuccess)
        assert result.features == [
            {
                "id": "sample_feature",
                "name": "Sample feature",
                "description": "Does something experimental.",
                "default": False,
                "owner": "@someone",
                "remove_by": "2027-01-31",
                "config_key": "beta_features.sample_feature",
            }
        ]
