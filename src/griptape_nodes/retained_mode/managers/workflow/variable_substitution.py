from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.workflow_events import (
    GetVariableSubstitutionEnabledRequest,
    GetVariableSubstitutionEnabledResultSuccess,
    SetVariableSubstitutionEnabledRequest,
    SetVariableSubstitutionEnabledResultNotAlteredSuccess,
    SetVariableSubstitutionEnabledResultSuccess,
)
from griptape_nodes.retained_mode.request_handlers import RequestScope, handles

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime


logger = logging.getLogger("griptape_nodes")


class VariableSubstitution(EngineScoped):
    """The open workflow's variable substitution flag, which its runtime holds.

    It cannot live in WorkflowMetadata because build_workflow() must set it during both editor
    loads and direct script execution.
    """

    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

    def is_enabled(self) -> bool:
        """Return whether variable substitution is enabled for the open workflow.

        Defaults to True, so workflows that have never set the flag get substitution without
        any migration, and so does work with no workflow open.
        """
        workflow = self.engine.current_workflow_or_none()
        if workflow is None:
            return True
        return workflow.variables.substitution_enabled

    @handles(GetVariableSubstitutionEnabledRequest, scope=RequestScope.WORKFLOW)
    def on_get_variable_substitution_enabled_request(
        self,
        request: GetVariableSubstitutionEnabledRequest,  # noqa: ARG002
        workflow: WorkflowRuntime,
    ) -> ResultPayload:
        """Return whether variable substitution is enabled for the workflow."""
        enabled = workflow.variables.substitution_enabled
        return GetVariableSubstitutionEnabledResultSuccess(
            result_details=f"Variable substitution is {'enabled' if enabled else 'disabled'} for the workflow.",
            enabled=enabled,
        )

    @handles(SetVariableSubstitutionEnabledRequest, scope=RequestScope.WORKFLOW)
    def on_set_variable_substitution_enabled_request(
        self, request: SetVariableSubstitutionEnabledRequest, workflow: WorkflowRuntime
    ) -> ResultPayload:
        """Enable or disable variable substitution for the workflow.

        Stores the flag on the open workflow's runtime. When the workflow is saved, the code
        generator bakes a SetVariableSubstitutionEnabledRequest call into build_workflow() so the
        flag is restored on every subsequent load, including direct script execution.
        """
        workflow_name = self.engine.workflow_registry.get(workflow.workflow_id).name
        workflow.variables.substitution_enabled = request.enabled
        details = (
            f"Variable substitution {'enabled' if request.enabled else 'disabled'} for workflow '{workflow_name}'."
        )
        if request.initial_setup:
            return SetVariableSubstitutionEnabledResultNotAlteredSuccess(result_details=details)
        return SetVariableSubstitutionEnabledResultSuccess(result_details=details)
