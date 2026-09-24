"""Engine beta features: experimental behavior users can turn on or off.

Every engine beta feature is registered in this module so they are easy to audit. Values live in
the config under `beta_features.<id>`, the same map the editor's Beta settings page writes, and
`ListBetaFeaturesRequest` is how the editor discovers the features registered here.

Register a feature at module level and check it where behavior diverges:

    PARALLEL_BRANCH_RESOLUTION = register_beta_feature(
        BetaFeature(
            id="parallel_branch_resolution",
            name="Parallel branch resolution",
            description="Runs independent branches of a flow at the same time instead of one after another.",
            owner="@some-engine-dev",
            remove_by=date(2027, 1, 31),
        )
    )

    if is_beta_enabled(PARALLEL_BRANCH_RESOLUTION, self.engine.config_manager):
        ...

`tests/unit/retained_mode/test_beta_features.py` fails once a feature passes its `remove_by` date
or sets one more than 180 days out. See the "Beta features" section of CLAUDE.md.

Node libraries declare their own features in the `beta_features` list of their library JSON.
`parse_library_beta_features` turns those entries into `BetaFeature`s tagged with the library's
name, whose values live under `library_beta_features.<library slug>.<id>`.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError, computed_field

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.managers.config_manager import ConfigManager

logger = logging.getLogger("griptape_nodes")

# Config maps holding the user's values. Defined here rather than in managers.settings, which
# imports this module, so that library_registry can import this module without pulling in the
# settings module's import graph.
BETA_FEATURES_KEY = "beta_features"
LIBRARY_BETA_FEATURES_KEY = "library_beta_features"

# The longest a feature may stay in beta. Engine features are held to it by a unit test, library
# features by a warning when the library loads.
MAX_BETA_DAYS = 180


class BetaFeature(BaseModel):
    """One experimental feature, in the definition shape the editor shares.

    Attributes:
        id: snake_case identifier with single underscores. An engine feature's id is unique across
            the editor and the engine, because both store values in the shared `beta_features`
            map. A library feature's id only needs to be unique within its library, because its
            value is stored under that library in `library_beta_features`.
        name: Short label shown on the editor's Beta settings page.
        description: One or two sentences in user terms: what changes and where.
        default: Whether the feature is on when the user has not set it. Almost always False.
        owner: GitHub handle of whoever promotes or removes the feature.
        remove_by: Date by which the feature is promoted to default or deleted. Once it passes,
            the feature is hidden from the editor and always uses its default.
        library: Name of the node library that declares the feature, or None for an engine
            feature. Set by `parse_library_beta_features`, never by the library author.
        config_key: Dot-notation config key holding the user's value. Derived from `id` and
            `library`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Words joined by single underscores. "__" separates path parts in GTN_CONFIG_ variable names,
    # so an id containing it couldn't be set from the environment.
    id: str = Field(pattern=r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")
    name: str
    description: str
    default: bool = False
    owner: str
    remove_by: date
    library: str | None = None

    @computed_field
    @property
    def config_key(self) -> str:
        """Dot-notation config key holding the user's value.

        Sent to the editor so it never builds the key itself. Library features live under their
        own map, keyed by library, so two libraries can use the same feature id.
        """
        if self.library is None:
            return f"{BETA_FEATURES_KEY}.{self.id}"

        return f"{LIBRARY_BETA_FEATURES_KEY}.{library_config_slug(self.library)}.{self.id}"

    def is_expired(self) -> bool:
        """Whether today is past `remove_by`."""
        return _today() > self.remove_by

    def is_remove_by_too_far_out(self) -> bool:
        """Whether `remove_by` is more than MAX_BETA_DAYS from today."""
        return self.remove_by > _today() + timedelta(days=MAX_BETA_DAYS)


class LibraryBetaFeatureIssue(NamedTuple):
    """A `beta_features` entry in a library JSON that could not be used.

    Attributes:
        feature_id: The entry's `id`, or a placeholder naming its position when it has none.
        reason: What is wrong, phrased to follow "Beta feature '<id>' ".
    """

    feature_id: str
    reason: str


class ParsedLibraryBetaFeatures(NamedTuple):
    """The result of `parse_library_beta_features`.

    Attributes:
        features: Every valid entry, including expired ones, keyed by feature id.
        issues: One per entry that was dropped.
    """

    features: dict[str, BetaFeature]
    issues: list[LibraryBetaFeatureIssue]


# Keys a library JSON entry may set. `library` is left out because the engine fills it in.
_LIBRARY_ENTRY_FIELDS = frozenset(BetaFeature.model_fields) - {"library"}


_registry: dict[str, BetaFeature] = {}


def register_beta_feature(feature: BetaFeature) -> BetaFeature:
    """Add a feature to the registry and return it, so it can be bound to a module constant.

    Raises:
        ValueError: A feature with the same id is already registered.
    """
    if feature.id in _registry:
        msg = f"Attempted to register beta feature '{feature.id}'. Failed because a feature with that id is already registered."
        raise ValueError(msg)

    _registry[feature.id] = feature
    return feature


def list_beta_features() -> list[BetaFeature]:
    """Every registered feature, in registration order."""
    return list(_registry.values())


def get_beta_feature(feature_id: str) -> BetaFeature | None:
    """The registered engine feature with this id, or None."""
    return _registry.get(feature_id)


def parse_library_beta_features(library_name: str, entries: list[Any]) -> ParsedLibraryBetaFeatures:
    """Turn the `beta_features` list of a library JSON into features tagged with the library's name.

    Each entry is checked on its own, so one mistake drops that entry and leaves the rest of the
    library, and its other features, working. Callers report the issues as library problems.

    Keys this engine doesn't know are ignored rather than rejected, so a library that uses a field
    added in a later engine still loads its features here.
    """
    features: dict[str, BetaFeature] = {}
    issues: list[LibraryBetaFeatureIssue] = []
    if not library_config_slug(library_name):
        # Without letters or digits in the name there is no key to store the user's choices under.
        for index, entry in enumerate(entries):
            feature_id = f"#{index + 1}"
            if isinstance(entry, dict) and entry.get("id"):
                feature_id = str(entry["id"])
            issues.append(
                LibraryBetaFeatureIssue(
                    feature_id,
                    "can't be used, because the library's name has no letters or digits to store its settings "
                    "under. Add some to the library name",
                )
            )
        return ParsedLibraryBetaFeatures(features=features, issues=issues)

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            issues.append(
                LibraryBetaFeatureIssue(f"#{index + 1}", f"is not an object with id, name, and so on, got {entry!r}")
            )
            continue

        feature_id = str(entry.get("id") or f"#{index + 1}")
        unknown_keys = sorted(key for key in entry if key not in _LIBRARY_ENTRY_FIELDS)
        if unknown_keys:
            logger.debug(
                "Ignoring unrecognized keys %s on beta feature '%s' of library '%s'.",
                unknown_keys,
                feature_id,
                library_name,
            )
        known = {key: value for key, value in entry.items() if key in _LIBRARY_ENTRY_FIELDS}
        try:
            feature = BetaFeature.model_validate({**known, "library": library_name})
        except ValidationError as e:
            fields = ", ".join(sorted({".".join(str(part) for part in error["loc"]) for error in e.errors()}))
            issues.append(LibraryBetaFeatureIssue(feature_id, f"has missing or invalid fields: {fields}"))
            continue

        if feature.id in features:
            issues.append(LibraryBetaFeatureIssue(feature_id, "is declared more than once. Only the first is used"))
            continue

        features[feature.id] = feature
    return ParsedLibraryBetaFeatures(features=features, issues=issues)


def find_beta_feature_date_issues(features: list[BetaFeature]) -> list[LibraryBetaFeatureIssue]:
    """Report features past their `remove_by` date, or with one more than MAX_BETA_DAYS away.

    Engine features are held to the same rules by a unit test. Library features are checked when
    the library loads, because the engine's tests never see them.
    """
    issues: list[LibraryBetaFeatureIssue] = []
    for feature in features:
        if feature.is_expired():
            issues.append(
                LibraryBetaFeatureIssue(
                    feature.id,
                    f"passed its remove_by date of {feature.remove_by}. It is hidden from the Beta Features page "
                    "and uses its default. Make it a standard feature or remove it",
                )
            )
        elif feature.is_remove_by_too_far_out():
            issues.append(
                LibraryBetaFeatureIssue(
                    feature.id,
                    f"has a remove_by date of {feature.remove_by}, more than {MAX_BETA_DAYS} days away. "
                    "Pick a nearer date",
                )
            )
    return issues


def find_library_config_slug_collision(library_name: str, other_library_names: list[str]) -> str | None:
    """The first other library whose beta feature settings would share `library_name`'s config key.

    "My Library" and "my-library" both store their values under `library_beta_features.my_library`,
    so a feature id they both declare would share one toggle.
    """
    slug = library_config_slug(library_name)
    for other_name in other_library_names:
        if other_name != library_name and library_config_slug(other_name) == slug:
            return other_name
    return None


def is_beta_enabled(feature: BetaFeature, config_manager: ConfigManager) -> bool:
    """Whether the user has this feature on, falling back to `feature.default`.

    Takes the config manager rather than reaching the GriptapeNodes facade, which engine-internal
    code must not use. Callers pass `self.engine.config_manager`.

    Only a real boolean counts as a value; anything else means "use the default". The editor
    applies the same rule, so both sides agree on what is on. `Settings` drops non-boolean
    entries during validation, but the merged config keeps the raw values, so this check is
    what actually protects the read. It deliberately avoids `get_config_value(cast_type=bool)`,
    which turns any unrecognized string such as `"maybe"` into True. Environment variables are
    already booleans here, because the env layer coerces them through `Settings`.

    An expired feature always uses its default. The editor hides it, so the user could no longer
    see or change a value they had set.
    """
    if feature.is_expired():
        return feature.default

    value = config_manager.get_config_value(feature.config_key, should_load_env_var_if_detected=False)
    if not isinstance(value, bool):
        return feature.default

    return value


def library_config_slug(library_name: str) -> str:
    """The key a library's features are stored under in `library_beta_features`.

    Lowercase, with each run of spaces and punctuation replaced by one underscore, so
    "My Library" becomes "my_library". This keeps the key usable in dot-notation config keys and
    in GTN_CONFIG_ variable names, where "__" separates path parts.
    """
    return re.sub(r"[^a-z0-9]+", "_", library_name.lower()).strip("_")


def _today() -> date:
    return datetime.now(tz=UTC).date()
