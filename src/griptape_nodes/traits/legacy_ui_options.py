"""Rebuilding the traits a workflow saved before trait state was carried in its own right.

Such a file recorded a parameter's controls only as the flat ``ui_options`` keys the trait
rendered, because that was the only field a save wrote.

A parameter the node declares gets its trait back from the node's own ``__init__``, and the
saved keys are adopted onto it. A parameter created at run time has no such source: the trait
existed only in the file, as those keys. Left unread it loads as a bare field that still looks
like a dropdown, because the editor renders one from the stored keys, while the converter and
validator that made the choices mean anything are gone.

Reading them back is decoding a known-lossy format rather than guessing, and only because the
request says the file predates trait state. A current save always writes a ``traits`` list, so
an empty one there means the parameter genuinely has none and is left alone.
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
    """A trait a legacy file can be read back into, and the keys that identify it."""

    trait_class: type[Trait]
    # Keys a save wrote if and only if this trait was attached. Kept separate from everything
    # the trait claims through state_from_ui_options, because a secondary key on its own says
    # nothing: a parameter can carry `show_search` with no dropdown behind it.
    identifying_keys: tuple[str, ...]


# Every trait whose options a save could round-trip through ui_options, which is the same set
# the editor writes flat. A trait outside it was only ever built by node code, so a legacy
# file holding its rendered keys has a declared parameter behind them, not a lost trait.
LEGACY_CONTROLS: tuple[LegacyControl, ...] = (
    # enum_choices predates the Options trait, from when a dropdown was a SimpleDropdown option.
    LegacyControl(trait_class=Options, identifying_keys=("simple_dropdown", "enum_choices")),
    LegacyControl(trait_class=MultiOptions, identifying_keys=("multi_options",)),
    LegacyControl(trait_class=Slider, identifying_keys=("slider",)),
)


def reconstruct_traits_from_ui_options(parameter: Parameter) -> list[Trait]:
    """Attach the traits a pre-trait-state file recorded only as ``ui_options`` keys.

    Returns what was attached, so a caller can report it. A trait class already on the
    parameter is skipped: it came from live code, and the saved keys describe that instance
    rather than a second one to sit beside it.
    """
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
    """Build one trait from the options a legacy file stored, or None when they will not fit.

    A key present but holding something the trait's constructor rejects is a file written by a
    version that spelled the option differently. Losing that one control beats failing the
    load, which would cost the artist the whole workflow.
    """
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
