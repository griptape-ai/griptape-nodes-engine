"""Manufactures this library's node classes on demand.

The engine resolves a registered node type by importing the file named in its
`NodeDefinition.file_path` and calling `getattr(module, class_name)`. A module-level
`__getattr__` (PEP 562) is enough to satisfy that, so this one file backs every node
type in the library without declaring any of them as a `class` statement.

Classes are built on first lookup and cached in module globals, so repeated lookups
return the same object. That identity matters: the engine caches the resolved class
per node type, `isinstance` checks compare against it, and pickled parameter values
in saved workflows reference it by `__module__` + `__qualname__`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode

SPEC_FILE = Path(__file__).parent / "node_specs.json"


def load_operation_specs() -> list[dict[str, Any]]:
    """Read the node descriptions this module builds classes from.

    Deliberately duplicated from `advanced_library.py` instead of imported: the engine
    loads each library file as a standalone module under a mangled name, so importing a
    sibling by module name would create a second module object for the same file.
    """
    return json.loads(SPEC_FILE.read_text())["operations"]


def _apply_operation(operator: str, a: float, b: float, node_name: str) -> float:
    if operator == "add":
        return a + b
    if operator == "subtract":
        return a - b
    if operator == "multiply":
        return a * b
    if operator == "divide":
        if b == 0:
            msg = f"Attempted to divide by zero in '{node_name}'. Failed because the 'b' input is 0. Set 'b' to any non-zero number."
            raise ValueError(msg)
        return a / b
    msg = f"Attempted to run '{node_name}'. Failed because its operation '{operator}' is not one this library knows how to perform."
    raise ValueError(msg)


def _build_node_class(spec: dict[str, Any]) -> type[DataNode]:
    """Build a DataNode subclass for one operation spec."""
    operator = spec["operator"]
    symbol = spec["symbol"]

    def __init__(self: DataNode, name: str, metadata: dict | None = None) -> None:
        DataNode.__init__(self, name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="a",
                tooltip=f"Left operand of a {symbol} b",
                type="float",
                default_value=0.0,
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="b",
                tooltip=f"Right operand of a {symbol} b",
                type="float",
                default_value=0.0,
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="result",
                tooltip=f"Result of a {symbol} b",
                type="float",
                default_value=0.0,
                allowed_modes={ParameterMode.OUTPUT, ParameterMode.PROPERTY},
            )
        )

    def process(self: DataNode) -> None:
        a = float(self.get_parameter_value("a") or 0.0)
        b = float(self.get_parameter_value("b") or 0.0)
        self.parameter_output_values["result"] = _apply_operation(operator, a, b, self.name)

    return type(
        spec["class_name"],
        (DataNode,),
        {
            "__init__": __init__,
            "process": process,
            "__doc__": f"{spec['description']} Synthesized from node_specs.json.",
            # Set both explicitly. Without `__module__` in this namespace, class creation
            # reads `__name__` from the calling frame's globals, and because DataNode carries
            # ABCMeta that frame is inside the stdlib `abc` module -- the class would claim
            # `__module__ == "abc"`. Pickled parameter values in saved workflows are restored
            # by importing `__module__` and looking up `__qualname__` on it, so a wrong
            # `__module__` breaks reopening any workflow that carries such a value. Pointing
            # at this module routes that lookup back through `__getattr__`, and the engine
            # registers a stable-namespace alias for this file so it resolves across sessions.
            "__module__": __name__,
            "__qualname__": spec["class_name"],
        },
    )


def __getattr__(name: str) -> type[DataNode]:
    """Build and cache the node class named `name`, or raise AttributeError."""
    spec = next((s for s in load_operation_specs() if s["class_name"] == name), None)
    if spec is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)

    node_class = _build_node_class(spec)
    # Cache in module globals so the next lookup bypasses __getattr__ entirely and
    # every caller shares one class object.
    globals()[name] = node_class
    return node_class


def __dir__() -> list[str]:
    """Report the synthesized class names so `dir()` and tab-completion see them."""
    return sorted({*globals(), *(spec["class_name"] for spec in load_operation_specs())})
