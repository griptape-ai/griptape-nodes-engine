"""Backward compatibility for run_in_parallel to run_in_order parameter change.

This module handles the parameter name change from 'run_in_parallel' to 'run_in_order'
in ForLoopStartNode and ForEachStartNode. The logic is inverted: run_in_parallel=True
becomes run_in_order=False (run in parallel means NOT run in order).

TODO: Remove this compatibility check once all workflows have been migrated and we no
longer need to support loading old workflows with the run_in_parallel parameter.
Link: https://github.com/griptape-ai/griptape-nodes-engine/issues/XXXX
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueResultSuccess
from griptape_nodes.retained_mode.managers.version_compatibility_manager import (
    SetParameterVersionCompatibilityCheck,
)

if TYPE_CHECKING:
    from griptape_nodes.exe_types.node_types import BaseNode


class RunInParallelToRunInOrderCheck(SetParameterVersionCompatibilityCheck):
    """Handle migration from run_in_parallel to run_in_order parameter.

    This check intercepts attempts to set the old 'run_in_parallel' parameter
    and converts it to the new 'run_in_order' parameter with inverted boolean logic.

    Applies to:
    - ForLoopStartNode
    - ForEachStartNode

    Migration logic:
    - run_in_parallel=True → run_in_order=False (run in parallel)
    - run_in_parallel=False → run_in_order=True (run sequentially)
    """

    # The standard library: https://github.com/griptape-ai/griptape-nodes-library-standard
    LIBRARY_NAME: ClassVar[str] = "griptape_nodes_library"

    def applies_to_set_parameter(self, node: BaseNode, parameter_name: str, _value: Any) -> bool:
        """Return True if this is the old run_in_parallel parameter on an affected node.

        Args:
            node: The node instance
            parameter_name: Name of the parameter being set
            _value: The value being set (unused)

        Returns:
            True if this check should handle this parameter
        """
        if parameter_name != "run_in_parallel":
            return False

        if self.get_node_library_name(node) != self.LIBRARY_NAME:
            return False

        node_type_name = type(node).__name__
        return node_type_name in ("ForLoopStartNode", "ForEachStartNode")

    def set_parameter_value(self, node: BaseNode, parameter_name: str, value: Any) -> SetParameterValueResultSuccess:
        """Migrate run_in_parallel to run_in_order with inverted boolean value.

        Args:
            node: The node instance
            parameter_name: Name of the parameter being set (should be "run_in_parallel")
            value: The value being set (boolean)

        Returns:
            SetParameterValueResultSuccess indicating the migration was successful
        """
        if value is None:
            inverted_value = True
        else:
            inverted_value = not bool(value)

        node.set_parameter_value("run_in_order", inverted_value)

        return SetParameterValueResultSuccess(
            finalized_value=inverted_value,
            data_type="bool",
            result_details=f"Migrated deprecated '{parameter_name}' parameter to 'run_in_order' with inverted value",
        )
