"""Advanced library that synthesizes this library's node definitions at load time.

`griptape_nodes_library.json` declares `"nodes": []`. Every node type this library
offers is described in `node_specs.json` instead, and this module turns those
descriptions into `NodeDefinition` objects during `before_library_nodes_loaded`.

The engine calls that hook before it iterates `library_data.nodes`, and `library_data`
is the same object the `Library` holds, so definitions appended here go through the
engine's normal node loader. See the Advanced Libraries guide for the full explanation
and for the limitations of this pattern.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from griptape_nodes.node_library.advanced_node_library import AdvancedNodeLibrary
from griptape_nodes.node_library.library_registry import NodeDefinition, NodeMetadata

if TYPE_CHECKING:
    from griptape_nodes.node_library.library_registry import Library, LibrarySchema

# Category key declared in griptape_nodes_library.json's `categories`.
CATEGORY = "dynamic"

# Every synthesized definition points at this one module. The engine resolves a node
# class with `getattr(module, class_name)`, and that module's `__getattr__` manufactures
# the class on demand.
FACTORY_FILE = "generated_nodes.py"

SPEC_FILE = Path(__file__).parent / "node_specs.json"


def load_operation_specs() -> list[dict[str, Any]]:
    """Read the node descriptions this library builds its node types from.

    `generated_nodes.py` reads the same file with its own copy of this function rather
    than importing it from here. The engine loads each library file as a standalone
    module under a mangled name, so importing a sibling by module name would produce a
    second module object for the same file, which breaks `isinstance` checks and pickling.
    """
    return json.loads(SPEC_FILE.read_text())["operations"]


class DynamicNodesDemoLibrary(AdvancedNodeLibrary):
    """Registers one node type per entry in `node_specs.json`.

    The engine instantiates this class with no arguments, so any state it needs must be
    derived here or in the hooks.
    """

    def before_library_nodes_loaded(self, library_data: LibrarySchema, library: Library) -> None:  # noqa: ARG002
        """Append one NodeDefinition per operation spec, before the engine's loader runs."""
        library_data.nodes.extend(
            NodeDefinition(
                class_name=spec["class_name"],
                file_path=FACTORY_FILE,
                metadata=NodeMetadata(
                    category=CATEGORY,
                    description=spec["description"],
                    display_name=spec["display_name"],
                    icon="Sigma",
                ),
            )
            for spec in load_operation_specs()
        )
