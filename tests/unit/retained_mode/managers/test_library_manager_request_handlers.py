"""Tests for get_request_handlers() registration in attempt_load_nodes_from_library (#4744)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Never
from unittest.mock import MagicMock, patch

from griptape_nodes.node_library.advanced_node_library import AdvancedNodeLibrary
from griptape_nodes.node_library.library_registry import Library, LibrarySchema
from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import RequestHandlerRegistrationProblem
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager
from griptape_nodes.retained_mode.request_handlers import RequestScope

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime


@dataclass
class _TestRequest(RequestPayload):
    pass


@dataclass
class _TestResult(ResultPayload):
    def succeeded(self) -> bool:
        return True


def _handler(_req: _TestRequest) -> _TestResult:
    return _TestResult(result_details="ok")


def _make_library_info() -> LibraryManager.LibraryInfo:
    return LibraryManager.LibraryInfo(
        lifecycle_state=LibraryManager.LibraryLifecycleState.EVALUATED,
        fitness=LibraryManager.LibraryFitness.NOT_EVALUATED,
        library_path="/fake/path",
        is_sandbox=False,
        library_name="TestLib",
        library_version="1.0.0",
    )


def _open_workflow(engine: Engine) -> WorkflowRuntime:
    engine.context_manager.push_workflow(workflow_name="open")
    return engine.open_workflows.get(engine.context_manager.get_current_workflow_id())


def _load(engine: Engine, handler: Callable[..., _TestResult]) -> LibraryManager.LibraryInfo:
    """Load a library whose only request handler is `handler`, for `_TestRequest`."""

    class HandlerLib(AdvancedNodeLibrary):
        def get_request_handlers(self) -> list:
            return [(_TestRequest, handler)]

    library = _make_library(advanced_library=HandlerLib())
    library_info = _make_library_info()
    engine.library_manager.module_loading.attempt_load_nodes_from_library(
        library_data=library._library_data,
        library=library,
        base_dir=Path("/fake"),
        library_info=library_info,
    )
    return library_info


def _make_library(advanced_library: AdvancedNodeLibrary | None = None) -> Library:
    schema = MagicMock(spec=LibrarySchema)
    schema.is_default_library = False
    schema.name = "TestLib"
    schema.nodes = []
    schema.workflow_nodes = []
    schema.widgets = []
    schema.config_categories = []
    return Library(library_data=schema, advanced_library=advanced_library)


class TestRequestHandlerRegistration:
    def test_handlers_registered_via_event_manager(self, engine: Engine) -> None:
        """get_request_handlers() pairs should be registered with EventManager."""

        class MyLib(AdvancedNodeLibrary):
            def get_request_handlers(self) -> list:
                return [(_TestRequest, _handler)]

        library = _make_library(advanced_library=MyLib())
        library_info = _make_library_info()

        lm = engine.library_manager
        event_manager = MagicMock()
        with patch.object(engine, "_event_manager", event_manager):
            lm.module_loading.attempt_load_nodes_from_library(
                library_data=library._library_data,
                library=library,
                base_dir=Path("/fake"),
                library_info=library_info,
            )

        event_manager.assign_manager_to_request_type.assert_called_once_with(
            _TestRequest, _handler, without_workflow=None
        )

    def test_handler_types_recorded_on_library(self, engine: Engine) -> None:
        """Registered handler types must be stored in library._registered_request_handler_types."""

        class MyLib(AdvancedNodeLibrary):
            def get_request_handlers(self) -> list:
                return [(_TestRequest, _handler)]

        library = _make_library(advanced_library=MyLib())
        library_info = _make_library_info()

        lm = engine.library_manager
        event_manager = MagicMock()
        with patch.object(engine, "_event_manager", event_manager):
            lm.module_loading.attempt_load_nodes_from_library(
                library_data=library._library_data,
                library=library,
                base_dir=Path("/fake"),
                library_info=library_info,
            )

        assert _TestRequest in library._registered_request_handler_types

    def test_exception_in_get_request_handlers_appends_problem(self, engine: Engine) -> None:
        """An exception from get_request_handlers() should append RequestHandlerRegistrationProblem."""

        class BoomLib(AdvancedNodeLibrary):
            def get_request_handlers(self) -> Never:
                msg = "handler registration exploded"
                raise RuntimeError(msg)

        library = _make_library(advanced_library=BoomLib())
        library_info = _make_library_info()
        # Add a dummy node so the library isn't marked UNUSABLE (any_nodes_loaded_successfully=False)
        library_info.problems = []

        lm = engine.library_manager
        event_manager = MagicMock()
        with patch.object(engine, "_event_manager", event_manager):
            lm.module_loading.attempt_load_nodes_from_library(
                library_data=library._library_data,
                library=library,
                base_dir=Path("/fake"),
                library_info=library_info,
            )

        problem_types = [type(p) for p in library_info.problems]
        assert RequestHandlerRegistrationProblem in problem_types

    def test_a_handler_with_no_workflow_parameter_acts_on_the_ambient_workflow(self, engine: Engine) -> None:
        """A library written before scopes keeps reading the open workflow, rather than becoming HOST."""

        def handler(request: _TestRequest) -> _TestResult:  # noqa: ARG001
            current = engine.current_workflow_or_none()
            return _TestResult(result_details=current.workflow_id if current is not None else "none")

        workflow = _open_workflow(engine)
        library_info = _load(engine, handler)

        assert library_info.problems == []
        assert engine.event_manager.get_scope_for_request_type(_TestRequest) is None
        assert str(engine.handle_request(_TestRequest()).result_details) == workflow.workflow_id

    def test_a_handler_with_a_workflow_parameter_is_handed_the_workflow(self, engine: Engine) -> None:
        def handler(request: _TestRequest, workflow: WorkflowRuntime) -> _TestResult:  # noqa: ARG001
            return _TestResult(result_details=workflow.workflow_id)

        workflow = _open_workflow(engine)
        library_info = _load(engine, handler)

        assert library_info.problems == []
        assert engine.event_manager.get_scope_for_request_type(_TestRequest) is RequestScope.WORKFLOW
        assert str(engine.handle_request(_TestRequest()).result_details) == workflow.workflow_id

    def test_a_workflow_parameter_with_another_default_is_a_registration_problem(self, engine: Engine) -> None:
        """`workflow` is the engine's parameter name, so a library can't use it for its own value."""

        def handler(request: _TestRequest, workflow: str = "mine") -> _TestResult:  # noqa: ARG001
            return _TestResult(result_details=workflow)

        library_info = _load(engine, handler)

        assert len(library_info.problems) == 1
        assert isinstance(library_info.problems[0], RequestHandlerRegistrationProblem)
        assert "defaults to 'mine'" in library_info.problems[0].error_message
        assert engine.event_manager.get_manager_for_request_type(_TestRequest) is None

    def test_no_advanced_library_does_nothing(self, engine: Engine) -> None:
        """A library with no advanced library should not touch EventManager at all."""
        library = _make_library(advanced_library=None)
        library_info = _make_library_info()

        lm = engine.library_manager
        event_manager = MagicMock()
        with patch.object(engine, "_event_manager", event_manager):
            lm.module_loading.attempt_load_nodes_from_library(
                library_data=library._library_data,
                library=library,
                base_dir=Path("/fake"),
                library_info=library_info,
            )

        event_manager.assign_manager_to_request_type.assert_not_called()

    def test_empty_get_request_handlers_does_nothing(self, engine: Engine) -> None:
        """A library returning [] from get_request_handlers() should not register any handlers."""

        class EmptyLib(AdvancedNodeLibrary):
            pass  # base default returns []

        library = _make_library(advanced_library=EmptyLib())
        library_info = _make_library_info()

        lm = engine.library_manager
        event_manager = MagicMock()
        with patch.object(engine, "_event_manager", event_manager):
            lm.module_loading.attempt_load_nodes_from_library(
                library_data=library._library_data,
                library=library,
                base_dir=Path("/fake"),
                library_info=library_info,
            )

        event_manager.assign_manager_to_request_type.assert_not_called()

    def test_a_library_that_executes_in_a_worker_still_registers_its_handlers(self, engine: Engine) -> None:
        """Handlers are registered by whichever process loaded the library, with no problem raised.

        Both processes load the library, so each registers its own copy and serves the requests
        dispatched to it. Where the nodes execute says nothing about that.
        """

        class WorkerLib(AdvancedNodeLibrary):
            def get_request_handlers(self) -> list:
                return [(_TestRequest, _handler)]

        library = _make_library(advanced_library=WorkerLib())
        library_info = _make_library_info()
        library_info.executes_in_worker = True

        lm = engine.library_manager
        event_manager = MagicMock()
        with patch.object(engine, "_event_manager", event_manager):
            lm.module_loading.attempt_load_nodes_from_library(
                library_data=library._library_data,
                library=library,
                base_dir=Path("/fake"),
                library_info=library_info,
            )

        event_manager.assign_manager_to_request_type.assert_called_once_with(
            _TestRequest, _handler, without_workflow=None
        )
        assert library_info.problems == []
