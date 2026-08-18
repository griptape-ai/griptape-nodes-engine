"""Check for deprecated job_group and execution_environment parameters."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

import semver

from griptape_nodes.exe_types.node_groups import SubflowNodeGroup
from griptape_nodes.retained_mode.events.app_events import (
    GetEngineVersionRequest,
    GetEngineVersionResultSuccess,
)
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueResultSuccess
from griptape_nodes.retained_mode.managers.version_compatibility_manager import (
    SetParameterVersionCompatibilityCheck,
)

if TYPE_CHECKING:
    from griptape_nodes.exe_types.node_types import BaseNode
    from griptape_nodes.retained_mode.engine import Engine

logger = logging.getLogger("griptape_nodes")


class DeprecatedNodeGroupParametersCheck(SetParameterVersionCompatibilityCheck):
    """Check for deprecated job_group and execution_environment parameters.

    These parameters were removed in engine version 0.63.8 for all nodes except NodeGroup.
    This check intercepts attempts to set these parameters and logs a warning prompting
    users to resave their workflows.
    """

    DEPRECATED_PARAMETERS: ClassVar[set[str]] = {"job_group", "execution_environment"}
    REMOVAL_VERSION: ClassVar[semver.VersionInfo] = semver.VersionInfo(0, 63, 8)

    def __init__(self, engine: Engine | None = None) -> None:
        """Initialize the check with an empty set of warned workflows."""
        super().__init__(engine)
        self._warned_workflows: set[str] = set()

    def applies_to_set_parameter(self, node: BaseNode, parameter_name: str, _value: Any) -> bool:
        """Return True if this is a deprecated parameter on a non-NodeGroup node.

        Args:
            node: The node instance
            parameter_name: Name of the parameter being set
            _value: The value being set (unused)

        Returns:
            True if this check should handle this parameter
        """
        # Check parameter name first (fastest check)
        if parameter_name not in self.DEPRECATED_PARAMETERS:
            return False

        # Check if node is NOT a NodeGroup (we only block for non-NodeGroup nodes)
        if isinstance(node, SubflowNodeGroup):
            return False

        # Check if current engine version is >= 0.63.8
        engine_version_result = self.engine.handle_request(GetEngineVersionRequest())
        if not isinstance(engine_version_result, GetEngineVersionResultSuccess):
            return False

        current_version = semver.VersionInfo(
            engine_version_result.major, engine_version_result.minor, engine_version_result.patch
        )
        return current_version >= self.REMOVAL_VERSION

    def set_parameter_value(self, _node: BaseNode, parameter_name: str, _value: Any) -> SetParameterValueResultSuccess:
        """Handle the deprecated parameter by logging a warning and returning success with empty value.

        Args:
            _node: The node instance (unused)
            parameter_name: Name of the parameter being set
            _value: The value being set (unused)

        Returns:
            SetParameterValueResultSuccess with empty list value
        """
        # Get the current workflow name to track warnings per workflow
        workflow_name = self.engine.context_manager.get_current_workflow_name()

        # Check if we've already warned for this workflow
        if workflow_name not in self._warned_workflows:
            # Mark this workflow as warned
            self._warned_workflows.add(workflow_name)

            # Log warning with all deprecated parameters
            deprecated_params_list = ", ".join(f"'{param}'" for param in self.DEPRECATED_PARAMETERS)
            logger.warning(
                "This workflow uses deprecated parameters (%s) that were removed in engine version %s. "
                "Please resave your workflow and this warning will go away.",
                deprecated_params_list,
                self.REMOVAL_VERSION,
            )

        # Return success with None as the value (parameter doesn't exist, so no meaningful value to return)
        return SetParameterValueResultSuccess(
            finalized_value=None,
            data_type="any",
            result_details=f"Parameter '{parameter_name}' was removed in v{self.REMOVAL_VERSION}. Please resave this workflow.",
        )
