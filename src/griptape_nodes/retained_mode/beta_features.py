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
"""

from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, computed_field

from griptape_nodes.retained_mode.managers.settings import BETA_FEATURES_KEY

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.managers.config_manager import ConfigManager

logger = logging.getLogger("griptape_nodes")


class BetaFeature(BaseModel):
    """One experimental feature, in the definition shape the editor shares.

    Attributes:
        id: snake_case identifier, unique across the editor and the engine. Also the config key
            under `beta_features`.
        name: Short label shown on the editor's Beta settings page.
        description: One or two sentences in user terms: what changes and where.
        default: Whether the feature is on when the user has not set it. Almost always False.
        owner: GitHub handle of whoever promotes or removes the feature.
        remove_by: Date by which the feature is promoted to default or deleted.
        config_key: Dot-notation config key holding the user's value. Derived from `id`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str
    description: str
    default: bool = False
    owner: str
    remove_by: date

    @computed_field
    @property
    def config_key(self) -> str:
        """Dot-notation config key holding the user's value.

        Sent to the editor so it never builds the key itself. Library-defined features, if they
        are added later, will live under a different key, and an editor already in the field has
        to keep writing to the right place.
        """
        return f"{BETA_FEATURES_KEY}.{self.id}"


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
    """
    value = config_manager.get_config_value(f"{BETA_FEATURES_KEY}.{feature.id}", should_load_env_var_if_detected=False)
    if not isinstance(value, bool):
        return feature.default

    return value
