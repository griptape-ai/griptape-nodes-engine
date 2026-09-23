from __future__ import annotations

import json
import logging
import platform
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.node_library.library_registry import LibraryMetadata, LibraryRegistry, LibrarySchema
from griptape_nodes.retained_mode import beta_features as beta_features_module
from griptape_nodes.retained_mode.beta_features import (
    MAX_BETA_DAYS,
    BetaFeature,
    find_beta_feature_date_issues,
    find_library_config_slug_collision,
    is_beta_enabled,
    library_config_slug,
    list_beta_features,
    parse_library_beta_features,
    register_beta_feature,
)
from griptape_nodes.retained_mode.engine import current_engine
from griptape_nodes.retained_mode.events.config_events import (
    IsBetaFeatureEnabledRequest,
    IsBetaFeatureEnabledResultFailure,
    IsBetaFeatureEnabledResultSuccess,
    ListBetaFeaturesRequest,
    ListBetaFeaturesResultSuccess,
)
from griptape_nodes.retained_mode.managers.config_manager import ConfigManager
from griptape_nodes.retained_mode.managers.fitness_problems.libraries.invalid_beta_feature_problem import (
    InvalidBetaFeatureProblem,
)
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine

LIBRARY_NAME = "Beta Test Library"
LIBRARY_SLUG = "beta_test_library"
SKIP_ON_WINDOWS = pytest.mark.skipif(
    platform.system() == "Windows", reason="xdg_base_dirs cannot find XDG_CONFIG_HOME on Windows on GitHub Actions"
)


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
        latest = _today() + timedelta(days=MAX_BETA_DAYS)
        assert feature.remove_by <= latest, (
            f"Beta feature '{feature.id}' (owner {feature.owner}) has remove_by {feature.remove_by}, "
            f"more than {MAX_BETA_DAYS} days out. Pick a nearer date."
        )

    def test_has_description(self, feature: BetaFeature) -> None:
        assert feature.description.strip(), f"Beta feature '{feature.id}' (owner {feature.owner}) needs a description."


def _in_days(days: int) -> date:
    return _today() + timedelta(days=days)


def _make_feature(
    feature_id: str = "sample_feature", *, default: bool = False, remove_by: date | None = None
) -> BetaFeature:
    if remove_by is None:
        remove_by = _in_days(30)
    return BetaFeature(
        id=feature_id,
        name="Sample feature",
        description="Does something experimental.",
        default=default,
        owner="@someone",
        remove_by=remove_by,
    )


def _library_entry(feature_id: str = "fast_upscale", **overrides: object) -> dict[str, object]:
    """A `beta_features` entry as it appears in a library JSON."""
    entry: dict[str, object] = {
        "id": feature_id,
        "name": "Fast upscale",
        "description": "Adds a faster upscale mode.",
        "owner": "@library-author",
        "remove_by": _in_days(30).isoformat(),
    }
    entry.update(overrides)
    return entry


def _register_library(beta_features: list[object], name: str = LIBRARY_NAME) -> None:
    schema = LibrarySchema(
        name=name,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="test", description="test", library_version="1.0.0", engine_version="1.0.0", tags=[]
        ),
        categories=[],
        nodes=[],
        beta_features=beta_features,
    )
    LibraryRegistry.generate_new_library(library_data=schema)


def _write_user_config(user_config_path: Path, contents: dict) -> None:
    user_config_path.write_text(json.dumps(contents), encoding="utf-8")


@pytest.fixture
def empty_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap in an empty registry so tests can register features without touching the real one."""
    monkeypatch.setattr(beta_features_module, "_registry", {})


@pytest.fixture
def clear_libraries() -> Iterator[None]:
    """Empty the library registry, which keeps its state in ClassVars the engine reset does not touch."""
    LibraryRegistry._clear()
    yield
    LibraryRegistry._clear()


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


@SKIP_ON_WINDOWS
class TestIsBetaEnabled:
    @staticmethod
    def _manager_with_user_config(user_config_path: Path, contents: dict) -> ConfigManager:
        _write_user_config(user_config_path, contents)
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

    @pytest.mark.parametrize("default", [True, False])
    def test_expired_feature_uses_default(self, isolate_user_config: Path, *, default: bool) -> None:
        manager = self._manager_with_user_config(
            isolate_user_config, {"beta_features": {"sample_feature": not default}}
        )

        assert is_beta_enabled(_make_feature(default=default, remove_by=_in_days(-1)), manager) is default

    def test_library_feature_reads_its_library_map(self, isolate_user_config: Path) -> None:
        manager = self._manager_with_user_config(
            isolate_user_config,
            {"beta_features": {"fast_upscale": False}, "library_beta_features": {LIBRARY_SLUG: {"fast_upscale": True}}},
        )
        feature = parse_library_beta_features(LIBRARY_NAME, [_library_entry()]).features["fast_upscale"]

        assert is_beta_enabled(feature, manager) is True


class TestLibraryBetaFeatures:
    @pytest.mark.parametrize(
        ("library_name", "slug"),
        [
            ("My Library", "my_library"),
            ("Griptape Nodes Library", "griptape_nodes_library"),
            ("fal.ai / Tools!", "fal_ai_tools"),
            ("already_snake", "already_snake"),
            ("  Padded--Name  ", "padded_name"),
        ],
    )
    def test_library_config_slug(self, library_name: str, slug: str) -> None:
        assert library_config_slug(library_name) == slug

    def test_engine_and_library_features_use_different_keys(self) -> None:
        parsed = parse_library_beta_features("My Library", [_library_entry()])

        assert _make_feature("fast_upscale").config_key == "beta_features.fast_upscale"
        assert parsed.features["fast_upscale"].config_key == "library_beta_features.my_library.fast_upscale"

    def test_valid_entries_are_tagged_with_the_library(self) -> None:
        parsed = parse_library_beta_features(LIBRARY_NAME, [_library_entry("one"), _library_entry("two")])

        assert list(parsed.features) == ["one", "two"]
        assert all(feature.library == LIBRARY_NAME for feature in parsed.features.values())
        assert parsed.issues == []

    def test_the_manifest_cannot_claim_another_library(self) -> None:
        parsed = parse_library_beta_features(LIBRARY_NAME, [_library_entry(library="Some Other Library")])

        assert parsed.features["fast_upscale"].library == LIBRARY_NAME

    def test_unknown_keys_are_ignored(self) -> None:
        """A manifest written for a newer engine can carry fields this engine doesn't know yet."""
        parsed = parse_library_beta_features(LIBRARY_NAME, [_library_entry(added_in_a_later_engine="x")])

        assert list(parsed.features) == ["fast_upscale"]
        assert parsed.issues == []

    @pytest.mark.parametrize("library_name", ["!!!", "日本語ツール"])
    def test_a_name_with_no_letters_or_digits_drops_every_feature(self, library_name: str) -> None:
        parsed = parse_library_beta_features(library_name, [_library_entry("one"), "not an object"])

        assert parsed.features == {}
        assert [issue.feature_id for issue in parsed.issues] == ["one", "#2"]
        assert "no letters or digits" in parsed.issues[0].reason

    def test_date_issues(self) -> None:
        features = [
            _make_feature("fine"),
            _make_feature("expired", remove_by=_in_days(-1)),
            _make_feature("too_far", remove_by=_in_days(MAX_BETA_DAYS + 1)),
        ]

        issues = find_beta_feature_date_issues(features)

        assert [issue.feature_id for issue in issues] == ["expired", "too_far"]

    @pytest.mark.parametrize(
        ("others", "collision"),
        [
            ([], None),
            (["Other Library"], None),
            (["beta-test library"], "beta-test library"),
            ([LIBRARY_NAME], None),
        ],
    )
    def test_slug_collision(self, others: list[str], collision: str | None) -> None:
        assert find_library_config_slug_collision(LIBRARY_NAME, others) == collision

    def test_a_bad_entry_is_dropped_and_the_rest_kept(self) -> None:
        entries = [
            _library_entry("good"),
            _library_entry("no_date", remove_by=None),
            _library_entry("Bad-Id"),
            "not an object",
            _library_entry("good"),
        ]

        parsed = parse_library_beta_features(LIBRARY_NAME, entries)

        assert list(parsed.features) == ["good"]
        assert [issue.feature_id for issue in parsed.issues] == ["no_date", "Bad-Id", "#4", "good"]
        assert "remove_by" in parsed.issues[0].reason
        assert "more than once" in parsed.issues[3].reason

    def test_load_reports_problems_as_warnings(self, engine: Engine) -> None:
        schema = LibrarySchema(
            name=LIBRARY_NAME,
            library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
            metadata=LibraryMetadata(
                author="test", description="test", library_version="1.0.0", engine_version="1.0.0", tags=[]
            ),
            categories=[],
            nodes=[],
            beta_features=[
                _library_entry("fine"),
                _library_entry("expired", remove_by=_in_days(-1).isoformat()),
                _library_entry("too_far", remove_by=_in_days(MAX_BETA_DAYS + 1).isoformat()),
                _library_entry("broken", owner=None),
            ],
        )

        issues = engine.version_compatibility_manager.check_library_version_compatibility(schema)

        beta_issues = [issue for issue in issues if isinstance(issue.problem, InvalidBetaFeatureProblem)]
        assert sorted(issue.problem.feature_id for issue in beta_issues) == ["broken", "expired", "too_far"]  # pyright: ignore[reportAttributeAccessIssue]
        assert all(issue.severity == LibraryManager.LibraryFitness.FLAWED for issue in beta_issues)


@SKIP_ON_WINDOWS
@pytest.mark.usefixtures("empty_registry", "clear_libraries")
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
                "remove_by": _in_days(30).isoformat(),
                "library": None,
                "config_key": "beta_features.sample_feature",
            }
        ]

    def test_includes_library_features_and_hides_expired_ones(self) -> None:
        register_beta_feature(_make_feature())
        register_beta_feature(_make_feature("old_engine_feature", remove_by=_in_days(-1)))
        _register_library([_library_entry(), _library_entry("old_library_feature", remove_by=_in_days(-1).isoformat())])

        result = ConfigManager().on_handle_list_beta_features_request(ListBetaFeaturesRequest())

        assert isinstance(result, ListBetaFeaturesResultSuccess)
        assert [(f["id"], f["library"], f["config_key"]) for f in result.features] == [
            ("sample_feature", None, "beta_features.sample_feature"),
            ("fast_upscale", LIBRARY_NAME, f"library_beta_features.{LIBRARY_SLUG}.fast_upscale"),
        ]


class _BetaProbeNode(BaseNode):
    """A node from the test library that checks its library's beta feature."""


@SKIP_ON_WINDOWS
@pytest.mark.usefixtures("empty_registry", "clear_libraries")
class TestIsBetaFeatureEnabled:
    def test_engine_feature(self, isolate_user_config: Path) -> None:
        register_beta_feature(_make_feature())
        _write_user_config(isolate_user_config, {"beta_features": {"sample_feature": True}})

        result = current_engine().handle_request(IsBetaFeatureEnabledRequest(feature_id="sample_feature"))

        assert isinstance(result, IsBetaFeatureEnabledResultSuccess)
        assert result.enabled is True

    def test_unknown_engine_feature_fails(self) -> None:
        result = current_engine().handle_request(IsBetaFeatureEnabledRequest(feature_id="nope"))

        assert isinstance(result, IsBetaFeatureEnabledResultFailure)

    def test_unloaded_library_fails(self) -> None:
        result = current_engine().handle_request(
            IsBetaFeatureEnabledRequest(feature_id="fast_upscale", library_name="Not Loaded")
        )

        assert isinstance(result, IsBetaFeatureEnabledResultFailure)
        assert "isn't loaded" in str(result.result_details)

    @pytest.mark.parametrize("configured", [True, False])
    def test_node_reads_its_library_feature(self, isolate_user_config: Path, *, configured: bool) -> None:
        _register_library([_library_entry(default=not configured)])
        _write_user_config(isolate_user_config, {"library_beta_features": {LIBRARY_SLUG: {"fast_upscale": configured}}})
        node = _BetaProbeNode(name="probe", metadata={"library": LIBRARY_NAME})

        assert node.is_beta_feature_enabled("fast_upscale") is configured

    def test_node_uses_default_when_unset(self) -> None:
        _register_library([_library_entry(default=True)])
        node = _BetaProbeNode(name="probe", metadata={"library": LIBRARY_NAME})

        assert node.is_beta_feature_enabled("fast_upscale") is True

    def test_node_treats_undeclared_feature_as_off(self, caplog: pytest.LogCaptureFixture) -> None:
        _register_library([])
        node = _BetaProbeNode(name="probe", metadata={"library": LIBRARY_NAME})

        with caplog.at_level(logging.DEBUG):
            assert node.is_beta_feature_enabled("fast_upscale") is False

        assert "doesn't declare a valid beta feature" in caplog.text
        assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
