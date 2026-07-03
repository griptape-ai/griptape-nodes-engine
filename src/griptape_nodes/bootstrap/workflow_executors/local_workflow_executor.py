from __future__ import annotations

import logging
import os
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

from griptape_nodes.bootstrap.workflow_executors.workflow_executor import WorkflowExecutor
from griptape_nodes.common.macro_parser.core import ParsedMacro
from griptape_nodes.common.project_templates.situation import BuiltInSituation
from griptape_nodes.drivers.storage import StorageBackend
from griptape_nodes.exe_types.node_types import EndNode, StartNode
from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete
from griptape_nodes.retained_mode.events.base_events import (
    EventRequest,
    ExecutionGriptapeNodeEvent,
)
from griptape_nodes.retained_mode.events.execution_events import StartFlowRequest
from griptape_nodes.retained_mode.events.flow_events import (
    GetTopLevelFlowRequest,
    GetTopLevelFlowResultSuccess,
    SerializeFlowToCommandsRequest,
    SerializeFlowToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest
from griptape_nodes.retained_mode.events.project_events import (
    GetCurrentProjectRequest,
    GetCurrentProjectResultSuccess,
    GetPathForMacroRequest,
    GetPathForMacroResultSuccess,
    GetSituationRequest,
    GetSituationResultSuccess,
    LoadProjectTemplateRequest,
    LoadProjectTemplateResultSuccess,
    SetCurrentProjectRequest,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    RunWorkflowFromScratchRequest,
    SaveWorkflowFileFromSerializedFlowRequest,
    SaveWorkflowFileFromSerializedFlowResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

if TYPE_CHECKING:
    from argparse import ArgumentParser, Namespace
    from types import TracebackType

logger = logging.getLogger(__name__)


class LocalExecutorError(Exception):
    """Exception raised during local workflow execution."""


class LocalWorkflowExecutor(WorkflowExecutor):
    def __init__(  # noqa: PLR0913
        self,
        storage_backend: StorageBackend = StorageBackend.LOCAL,
        *,
        project_file_path: Path | None = None,
        skip_library_loading: bool = False,
        workflows_to_register: list[str] | None = None,
        save_on_failure_path: str | None = None,
        pickle_control_flow_result: bool = False,
    ):
        super().__init__(pickle_control_flow_result=pickle_control_flow_result)
        self._set_storage_backend(storage_backend=storage_backend)
        self._project_file_path = project_file_path
        self._skip_library_loading = skip_library_loading
        self._workflows_to_register = workflows_to_register or []
        self._save_on_failure_path = save_on_failure_path

    async def __aenter__(self) -> Self:
        """Async context manager entry: initialize queue and broadcast app initialization."""
        GriptapeNodes.EventManager().initialize_queue()

        # Activate the user-specified project BEFORE broadcasting AppInitializationComplete.
        # At this point ProjectManager._initialization_complete is still False, so the
        # project switch skips the heavy library reload that would otherwise clear the
        # flow/nodes already created at module import time.
        if self._project_file_path is not None:
            await self._load_project(self._project_file_path)

        await GriptapeNodes.EventManager().abroadcast_app_event(
            AppInitializationComplete(
                skip_library_loading=self._skip_library_loading, workflows_to_register=self._workflows_to_register
            )
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Async context manager exit."""
        # TODO: Broadcast shutdown https://github.com/griptape-ai/griptape-nodes/issues/2149
        if exc_val is not None and self._save_on_failure_path is not None:
            await self._save_failed_workflow(exc_val)

    def _get_workflow_name(self) -> str:
        try:
            context_manager = GriptapeNodes.ContextManager()
            return context_manager.get_current_workflow_name()
        except Exception as e:
            msg = f"Failed to get current workflow from context manager: {e}"
            logger.exception(msg)
            raise LocalExecutorError(msg) from e

    def _load_flow_for_workflow(self) -> str:
        try:
            context_manager = GriptapeNodes.ContextManager()
            return context_manager.get_current_flow().name
        except Exception as e:
            msg = f"Failed to get current flow from context manager: {e}"
            logger.exception(msg)
            raise LocalExecutorError(msg) from e

    def _set_storage_backend(self, storage_backend: StorageBackend) -> None:
        from griptape_nodes.retained_mode.managers.config_manager import ConfigManager

        try:
            config_manager = ConfigManager()
            config_manager.set_config_value(
                key="storage_backend",
                value=storage_backend,
            )
        except Exception as e:
            msg = f"Failed to set storage backend: {e}"
            logger.exception(msg)
            raise LocalExecutorError(msg) from e

    async def _load_project(self, project_file_path: Path) -> None:
        """Load a project template and set it as the active project."""
        load_result = await GriptapeNodes.ahandle_request(LoadProjectTemplateRequest(project_path=project_file_path))
        if not isinstance(load_result, LoadProjectTemplateResultSuccess):
            msg = f"Attempted to load project template from {project_file_path}. Failed with result: {load_result}"
            logger.exception(msg)
            raise LocalExecutorError(msg)

        set_result = await GriptapeNodes.ahandle_request(SetCurrentProjectRequest(project_id=load_result.project_id))
        if set_result.failed():
            msg = f"Attempted to set project {load_result.project_id} as current. Failed with result: {set_result}"
            logger.exception(msg)
            raise LocalExecutorError(msg)

        logger.info("Loaded and activated project template from %s", project_file_path)

    def _submit_output(self, output: dict) -> None:
        self.output = output

    async def _set_input_for_flow(self, flow_name: str, flow_input: dict[str, dict]) -> None:
        control_flow = GriptapeNodes.FlowManager().get_flow_by_name(flow_name)
        nodes = control_flow.nodes
        for node_name, node in nodes.items():
            if isinstance(node, StartNode):
                param_map: dict | None = flow_input.get(node_name)
                if param_map is not None:
                    for parameter_name, parameter_value in param_map.items():
                        set_parameter_value_request = SetParameterValueRequest(
                            parameter_name=parameter_name,
                            value=parameter_value,
                            node_name=node_name,
                        )
                        set_parameter_value_result = await GriptapeNodes.ahandle_request(set_parameter_value_request)

                        if set_parameter_value_result.failed():
                            msg = f"Failed to set parameter {parameter_name} for node {node_name}."
                            raise LocalExecutorError(msg)

    def _get_output_for_flow(self, flow_name: str) -> dict:
        control_flow = GriptapeNodes.FlowManager().get_flow_by_name(flow_name)
        nodes = control_flow.nodes
        output = {}
        for node_name, node in nodes.items():
            if isinstance(node, EndNode):
                output[node_name] = node.parameter_values
                # Parameter_output_values should also be included, and should take priority over parameter_values
                output[node_name].update(node.parameter_output_values)

        return output

    async def _load_workflow_from_path(self, workflow_path: str) -> None:
        """Load a workflow from a file path."""

        def _raise_load_error(msg: str) -> None:
            raise LocalExecutorError(msg)

        try:
            # Use the RunWorkflowFromScratchRequest to load the workflow
            request = RunWorkflowFromScratchRequest(file_path=workflow_path)
            result = await GriptapeNodes.ahandle_request(request)

            logger.info("Successfully loaded workflow from %s", workflow_path)
        except Exception as e:
            msg = f"Error loading workflow from path {workflow_path}: {e}"
            logger.exception(msg)
            raise LocalExecutorError(msg) from e

        if result.failed():
            msg = f"Failed to load workflow from path {workflow_path}"
            _raise_load_error(msg)

    async def _handle_event_request(self, event: EventRequest) -> None:
        """Handle EventRequest objects by processing them through GriptapeNodes."""
        await GriptapeNodes.ahandle_request(event.request)

    async def _handle_execution_event(
        self, event: ExecutionGriptapeNodeEvent, flow_name: str
    ) -> tuple[bool, Exception | None]:
        """Handle ExecutionGriptapeNodeEvent and return (is_finished, error)."""
        result_event = event.wrapped_event

        if type(result_event.payload).__name__ == "ControlFlowResolvedEvent":
            self._submit_output(self._get_output_for_flow(flow_name=flow_name))
            logger.info("Workflow finished!")
            return True, None
        if type(result_event.payload).__name__ == "ControlFlowCancelledEvent":
            msg = "Control flow cancelled"
            logger.error(msg)
            return True, LocalExecutorError(msg)

        return False, None

    async def aprepare_workflow_for_run(
        self,
        flow_input: Any,
        **kwargs: Any,
    ) -> str:
        """Prepares a local workflow for execution.

        This method sets up the environment for executing a workflow, including
        initializing event listeners, registering libraries, loading the user-defined
        workflow, and preparing the specified workflow for execution.
        Parameters:
            flow_input: Input data for the flow, typically a dictionary.

        Returns:
            str: The name of the prepared flow.
        """
        GriptapeNodes.EventManager().initialize_queue()

        # Load workflow from file if workflow_path is provided
        workflow_path = kwargs.get("workflow_path")
        if workflow_path:
            await self._load_workflow_from_path(workflow_path)

        # Load the flow
        flow_name = self._load_flow_for_workflow()
        # Now let's set the input to the flow
        await self._set_input_for_flow(flow_name=flow_name, flow_input=flow_input)

        return flow_name

    async def _resolve_failure_save_path(self, workflow_name: str) -> str | None:
        """Resolve the user-supplied save-on-failure path to an absolute path string.

        Empty string sentinel means "use project situation"; None means disabled.
        """
        if self._save_on_failure_path is None:
            return None

        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        file_name_base = f"{workflow_name}_failed_{timestamp}"
        workspace = GriptapeNodes.ConfigManager().workspace_path
        fallback = str(workspace / "failures" / f"{file_name_base}.py")

        if self._save_on_failure_path:
            # expanduser is a pure string operation with no I/O; ASYNC240 does not apply here.
            expanded = os.path.expanduser(self._save_on_failure_path)  # noqa: PTH111, ASYNC240
            user_path = Path(expanded)
            if user_path.is_absolute():
                resolved = str(user_path)
            else:
                current_project_result = await GriptapeNodes.ahandle_request(GetCurrentProjectRequest())
                if isinstance(current_project_result, GetCurrentProjectResultSuccess):
                    resolved = str(current_project_result.project_info.project_base_dir / user_path)
                else:
                    resolved = str(workspace / user_path)
            return resolved

        # Empty sentinel — use the save_failed_workflow situation
        situation_result = await GriptapeNodes.ahandle_request(
            GetSituationRequest(situation_name=BuiltInSituation.SAVE_FAILED_WORKFLOW)
        )
        if not isinstance(situation_result, GetSituationResultSuccess):
            logger.warning(
                "Could not find '%s' situation; falling back to workspace root.",
                BuiltInSituation.SAVE_FAILED_WORKFLOW,
            )
            return fallback

        parsed_macro = ParsedMacro(template=situation_result.situation.macro)
        path_result = await GriptapeNodes.ahandle_request(
            GetPathForMacroRequest(
                parsed_macro=parsed_macro,
                variables={"file_name_base": file_name_base, "file_extension": "py"},
            )
        )
        if not isinstance(path_result, GetPathForMacroResultSuccess):
            logger.warning("Could not resolve save_failed_workflow macro; falling back to workspace root.")
            return fallback

        return str(path_result.absolute_path)

    async def _save_failed_workflow(self, error: BaseException | str) -> None:
        """Serialize and save the current flow state to a file for post-mortem debugging.

        Never raises — logs and returns on any internal failure so the original
        workflow exception propagates unchanged.
        """
        if self._save_on_failure_path is None:
            return

        try:
            # _get_workflow_name() can fail if the context manager is in a bad state at failure time;
            # fall back to a generic name rather than letting that secondary failure surface.
            try:
                workflow_name = self._get_workflow_name()
            except Exception:
                logger.warning("save_failed_workflow: could not retrieve workflow name; using 'workflow'.")
                workflow_name = "workflow"

            top_level_flow_result = await GriptapeNodes.ahandle_request(GetTopLevelFlowRequest())
            if not isinstance(top_level_flow_result, GetTopLevelFlowResultSuccess):
                logger.error("save_failed_workflow: could not get top-level flow; skipping save.")
                return

            serialized_flow_result = await GriptapeNodes.ahandle_request(
                SerializeFlowToCommandsRequest(
                    flow_name=top_level_flow_result.flow_name,
                    include_create_flow_command=True,
                )
            )
            if not isinstance(serialized_flow_result, SerializeFlowToCommandsResultSuccess):
                logger.error("save_failed_workflow: could not serialize flow; skipping save.")
                return

            target_path = await self._resolve_failure_save_path(workflow_name)
            if target_path is None:
                return

            timestamp = datetime.now(UTC).isoformat()
            if isinstance(error, BaseException):
                error_text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
            else:
                error_text = str(error)
            description = f"Workflow failed at {timestamp}.\n\n{error_text}"

            file_name = Path(target_path).stem
            save_result = await GriptapeNodes.ahandle_request(
                SaveWorkflowFileFromSerializedFlowRequest(
                    serialized_flow_commands=serialized_flow_result.serialized_flow_commands,
                    file_name=file_name,
                    file_path=target_path,
                    description=description,
                )
            )
            if isinstance(save_result, SaveWorkflowFileFromSerializedFlowResultSuccess):
                logger.info("Saved failed workflow state to %s", save_result.file_path)
            else:
                logger.error("save_failed_workflow: save request failed: %s", save_result)
        except Exception:
            logger.exception("save_failed_workflow: unexpected error during failure save (original error unchanged).")

    async def arun(
        self,
        flow_input: Any,
        storage_backend: StorageBackend | None = None,  # noqa: ARG002
        *,
        pickle_control_flow_result: bool | None = None,
        **kwargs: Any,
    ) -> None:
        """Executes a local workflow.

        Executes a workflow by setting up event listeners, registering libraries,
        loading the user-defined workflow, and running the specified workflow.

        Parameters:
            workflow_name: The name of the workflow to execute.
            flow_input: Input data for the flow, typically a dictionary.
            storage_backend: Accepted for compatibility with the base-class run path,
                but ignored here: the storage backend is applied once at construction
                via `_set_storage_backend`. Passing it to the run path has no effect.
            pickle_control_flow_result: Per-call override for the executor's
                save-time default. None means "use the instance default".

        Returns:
            None
        """
        flow_name = await self.aprepare_workflow_for_run(
            flow_input=flow_input,
            **kwargs,
        )

        # Now send the run command to actually execute it
        effective_pickle = (
            pickle_control_flow_result if pickle_control_flow_result is not None else self._pickle_control_flow_result
        )
        start_flow_request = StartFlowRequest(flow_name=flow_name, pickle_control_flow_result=effective_pickle)
        start_flow_result = await GriptapeNodes.ahandle_request(start_flow_request)

        if start_flow_result.failed():
            msg = f"Failed to start flow {flow_name}"
            raise LocalExecutorError(msg)

        logger.info("Workflow started!")

        # Wait for the control flow to finish
        is_flow_finished = False
        error: Exception | None = None

        event_queue = GriptapeNodes.EventManager().event_queue
        while not is_flow_finished:
            try:
                event = await event_queue.get()

                if isinstance(event, EventRequest):
                    await self._handle_event_request(event)
                elif isinstance(event, ExecutionGriptapeNodeEvent):
                    is_flow_finished, error = await self._handle_execution_event(event, flow_name)

                event_queue.task_done()

            except Exception as e:
                msg = f"Error handling queue event: {e}"
                logger.info(msg)

        if error is not None:
            raise error

    @classmethod
    def add_cli_arguments(
        cls,
        parser: ArgumentParser,
        *,
        pickle_control_flow_result_default: bool = False,
    ) -> None:
        super().add_cli_arguments(parser, pickle_control_flow_result_default=pickle_control_flow_result_default)
        cls._add_save_on_failure_argument(parser)

    @classmethod
    def _add_save_on_failure_argument(cls, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--save-on-failure",
            nargs="?",
            const="",
            default=None,
            help=(
                "On failure, save the current workflow state as a .py file. "
                "With no value: uses the project 'save_failed_workflow' situation. "
                "With a value: absolute or project-relative path."
            ),
        )

    @classmethod
    def _cli_constructor_kwargs(cls, args: Namespace) -> dict[str, Any]:
        kwargs = super()._cli_constructor_kwargs(args)
        kwargs["save_on_failure_path"] = args.save_on_failure
        return kwargs
