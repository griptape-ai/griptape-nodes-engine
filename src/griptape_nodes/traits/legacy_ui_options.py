"""Reconstruct traits stored only as flat ``ui_options``.

A missing ``traits`` field identifies this legacy format. An empty list explicitly means no
traits. Runtime parameters need reconstruction because node code does not rebuild them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, NamedTuple

from griptape_nodes.exe_types.core_types import Trait
from griptape_nodes.traits.multi_options import MultiOptions
from griptape_nodes.traits.options import Options
from griptape_nodes.traits.slider import Slider

if TYPE_CHECKING:
    from griptape_nodes.exe_types.core_types import Parameter

logger = logging.getLogger("griptape_nodes")


class LegacyControl(NamedTuple):
    trait_class: type[Trait]
    # Secondary keys such as ``show_search`` do not identify a control alone.
    identifying_keys: tuple[str, ...]


# Controls recoverable from legacy flat options.
LEGACY_CONTROLS: tuple[LegacyControl, ...] = (
    # Legacy name for a dropdown.
    LegacyControl(trait_class=Options, identifying_keys=("simple_dropdown", "enum_choices")),
    LegacyControl(trait_class=MultiOptions, identifying_keys=("multi_options",)),
    LegacyControl(trait_class=Slider, identifying_keys=("slider",)),
)


def reconstruct_traits_from_ui_options(parameter: Parameter) -> list[Trait]:
    """Attach recoverable traits not already provided by node code."""
    ui_options = parameter.ui_options
    attached_classes = {type(trait) for trait in parameter.find_elements_by_type(Trait)}
    rebuilt: list[Trait] = []
    for control in LEGACY_CONTROLS:
        if control.trait_class in attached_classes:
            continue
        if not any(key in ui_options for key in control.identifying_keys):
            continue
        trait = _build_control(parameter, control, ui_options)
        if trait is None:
            continue
        parameter.add_trait(trait)
        rebuilt.append(trait)
    return rebuilt


def _build_control(parameter: Parameter, control: LegacyControl, ui_options: dict[str, Any]) -> Trait | None:
    """Build one trait without failing the workflow load on incompatible options."""
    state = control.trait_class.state_from_ui_options(ui_options)
    try:
        return control.trait_class.from_state(state)
    except (TypeError, ValueError):
        logger.warning(
            "Parameter '%s' was saved with a %s control, but its saved options do not fit that "
            "control any more, so the parameter loads without it. Re-adding the control on the "
            "node and saving again will fix it.",
            parameter.name,
            control.trait_class.__name__,
        )
        return None
