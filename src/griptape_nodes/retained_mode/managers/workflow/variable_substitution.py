from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.workflow_events import (
    GetVariableSubstitutionEnabledRequest,
    GetVariableSubstitutionEnabledResultFailure,
    GetVariableSubstitutionEnabledResultSuccess,
    SetVariableSubstitutionEnabledRequest,
    SetVariableSubstitutionEnabledResultFailure,
    SetVariableSubstitutionEnabledResultNotAlteredSuccess,
    SetVariableSubstitutionEnabledResultSuccess,
)
from griptape_nodes.retained_mode.request_handlers import handles

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager


logger = logging.getLogger("griptape_nodes")


class VariableSubstitution(EngineScoped):
    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        # Missing keys enable substitution. This cannot live in WorkflowMetadata because
        # build_workflow() must set it during both editor loads and direct script execution.
        self._enabled: dict[str, bool] = {}
        event_manager.register_request_handlers(self)

    def clear(self) -> None:
        """Clear all per-workflow substitution settings."""
        self._enabled.clear()

    def drop(self, workflow_key: str) -> None:
        """Remove the substitution flag when a workflow is permanently deleted."""
        self._enabled.pop(workflow_key, None)

    def rekey(self, old_key: str, new_key: str) -> None:
        """Transfer the substitution flag when a workflow registry key changes.

        Only moves the entry when a flag was explicitly set; workflows that defaulted
        to True (no dict entry) stay that way under the new key without polluting the dict.
        """
        if old_key in self._enabled:
            self._enabled[new_key] = self._enabled.pop(old_key)

    def is_enabled(self) -> bool:
        """Return whether variable substitution is enabled for the current workflow.

        Reads from the in-memory dict populated by SetVariableSubstitutionEnabledRequest.
        Defaults to True so existing workflows that have never set the flag get
        substitution without any migration.
        """
        context_manager = self.engine.context_manager
        if not context_manager.has_current_workflow():
            return True
        workflow_name = context_manager.get_current_workflow_name()
        # Return the stored value, or True if this workflow has never set the flag.
        return self._enabled.get(workflow_name, True)

    @handles(GetVariableSubstitutionEnabledRequest)
    def on_get_variable_substitution_enabled_request(
        self,
        request: GetVariableSubstitutionEnabledRequest,  # noqa: ARG002
    ) -> ResultPayload:
        """Return whether variable substitution is enabled for the current workflow."""
        context_manager = self.engine.context_manager
        if not context_manager.has_current_workflow():
            return GetVariableSubstitutionEnabledResultFailure(
                result_details="Attempted to get variable substitution enabled. Failed because no workflow is active."
            )
        enabled = self.is_enabled()
        return GetVariableSubstitutionEnabledResultSuccess(
            result_details=f"Variable substitution is {'enabled' if enabled else 'disabled'} for the current workflow.",
            enabled=enabled,
        )

    @handles(SetVariableSubstitutionEnabledRequest)
    def on_set_variable_substitution_enabled_request(
        self, request: SetVariableSubstitutionEnabledRequest
    ) -> ResultPayload:
        """Enable or disable variable substitution for the current workflow.

        Stores the flag in memory keyed by the current workflow name. When the
        workflow is saved, the code generator bakes a SetVariableSubstitutionEnabledRequest
        call into build_workflow() so the flag is restored on every subsequent load,
        including direct script execution.
        """
        context_manager = self.engine.context_manager
        if not context_manager.has_current_workflow():
            return SetVariableSubstitutionEnabledResultFailure(
                result_details="Attempted to set variable substitution enabled. Failed because no workflow is active."
            )
        workflow_name = context_manager.get_current_workflow_name()
        self._enabled[workflow_name] = request.enabled
        details = (
            f"Variable substitution {'enabled' if request.enabled else 'disabled'} for workflow '{workflow_name}'."
        )
        if request.initial_setup:
            return SetVariableSubstitutionEnabledResultNotAlteredSuccess(result_details=details)
        return SetVariableSubstitutionEnabledResultSuccess(result_details=details)
