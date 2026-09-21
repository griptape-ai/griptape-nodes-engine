"""Backward compatibility for removed Flux2ImageGeneration parameters.

This module handles removed parameters from Flux2ImageGeneration when adding
FLUX.2[max] support:
- prompt_upsampling: removed entirely
- aspect_ratio: replaced with explicit width/height parameters
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueResultSuccess
from griptape_nodes.retained_mode.managers.version_compatibility_manager import (
    SetParameterVersionCompatibilityCheck,
)

if TYPE_CHECKING:
    from griptape_nodes.exe_types.node_types import BaseNode
    from griptape_nodes.retained_mode.engine import Engine

logger = logging.getLogger("griptape_nodes")


class Flux2RemovedParametersCheck(SetParameterVersionCompatibilityCheck):
    """Handle removed prompt_upsampling and aspect_ratio parameters.

    These parameters were removed in engine version 0.65.5 when adding FLUX.2[max] support.
    This check intercepts attempts to set these parameters and logs a warning prompting
    users to resave their workflows.
    """

    REMOVED_PARAMETERS: ClassVar[set[str]] = {"prompt_upsampling", "aspect_ratio"}
    # The standard library: https://github.com/griptape-ai/griptape-nodes-library-standard
    LIBRARY_NAME: ClassVar[str] = "griptape_nodes_library"

    def __init__(self, engine: Engine | None = None) -> None:
        """Initialize the check with an empty set of warned workflows."""
        super().__init__(engine)
        self._warned_workflows: set[str] = set()

    def applies_to_set_parameter(self, node: BaseNode, parameter_name: str, _value: Any) -> bool:
        """Return True if this is a removed parameter on a Flux2ImageGeneration node.

        Args:
            node: The node instance
            parameter_name: Name of the parameter being set
            _value: The value being set (unused)

        Returns:
            True if this check should handle this parameter
        """
        if parameter_name not in self.REMOVED_PARAMETERS:
            return False

        if self.get_node_library_name(node) != self.LIBRARY_NAME:
            return False

        return type(node).__name__ == "Flux2ImageGeneration"

    def set_parameter_value(self, _node: BaseNode, parameter_name: str, _value: Any) -> SetParameterValueResultSuccess:
        """Handle the removed parameter by logging a warning and returning success.

        Args:
            _node: The node instance (unused)
            parameter_name: Name of the parameter being set
            _value: The value being set (unused)

        Returns:
            SetParameterValueResultSuccess with None value
        """
        workflow_name = self.engine.context_manager.get_current_workflow_name()

        if workflow_name not in self._warned_workflows:
            self._warned_workflows.add(workflow_name)

            removed_params_list = ", ".join(f"'{param}'" for param in self.REMOVED_PARAMETERS)
            logger.warning(
                "This workflow uses removed Flux2ImageGeneration parameters (%s) that were "
                "replaced in engine version 0.65.5. Please resave your workflow and this "
                "warning will go away.",
                removed_params_list,
            )

        return SetParameterValueResultSuccess(
            finalized_value=None,
            data_type="any",
            result_details=f"Parameter '{parameter_name}' was removed in v0.65.5. Please resave this workflow.",
        )
