"""Compatibility imports for node libraries; use ``griptape_nodes.retained_mode.events.converters``.

``converter`` is ``converters.engine``, so a hook registered on it does not reach ``converters.client``.
"""

import warnings
from typing import Any

from griptape_nodes.retained_mode.events import converters
from griptape_nodes.retained_mode.events.converters import register_polymorphic_dataclass

__all__ = ["converter", "register_polymorphic_dataclass", "safe_unstructure"]

converter = converters.engine


def safe_unstructure(obj: Any) -> Any:
    """Deprecated. Use ``griptape_nodes.serialization.values.encode_value`` for parameter values.

    Unstructures ``obj`` with the event converter. It no longer turns griptape objects into their
    ``to_dict()`` form, or a value it cannot unstructure into text.
    """
    warnings.warn(
        "safe_unstructure() is deprecated and will be removed in a later release. Use encode_value() from "
        "griptape_nodes.serialization.values to turn a parameter value into plain data.",
        DeprecationWarning,
        stacklevel=2,
    )
    return converter.unstructure(obj)
