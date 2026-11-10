"""File handles act on the workflow they were built for, from any thread.

A plain `threading.Thread` or a thread pool starts with no binding, so a handle that looked its
workflow up when it did I/O would act on whichever workflow was open, or on none.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest import mock

import pytest

from griptape_nodes.common.macro_parser import ParsedMacro
from griptape_nodes.exe_types.param_components.project_directory_parameter import ProjectDirectoryParameter
from griptape_nodes.exe_types.param_components.project_file_parameter import ProjectFileParameter
from griptape_nodes.exe_types.param_components.project_file_sequence_parameter import ProjectFileSequenceParameter
from griptape_nodes.files.directory import Directory, DirectoryDestination
from griptape_nodes.files.file import File, FileDestination
from griptape_nodes.files.file_sequence import FileSequence, FileSequenceDestination
from griptape_nodes.files.project_file import ProjectFileDestination
from griptape_nodes.retained_mode.events import destination_events
from griptape_nodes.retained_mode.events.os_events import (
    GetNextVersionIndexRequest,
    GetNextVersionIndexResultSuccess,
    WriteFileRequest,
    WriteFileResultSuccess,
)
from griptape_nodes.retained_mode.events.project_events import (
    AttemptMapAbsolutePathToProjectRequest,
    AttemptMapAbsolutePathToProjectResultSuccess,
    GetPathForMacroRequest,
    GetPathForMacroResultSuccess,
    GetSituationRequest,
    GetSituationResultFailure,
    MacroPath,
)
from griptape_nodes.retained_mode.runtime.binding import NO_WORKFLOW, bound_workflow

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime


class _RecordingWorkflow:
    """Stands in for a workflow runtime: answers file requests from its own folder and records each one."""

    def __init__(self, folder: str) -> None:
        self.folder = folder
        self.requests: list[RequestPayload] = []
        self.objects = mock.MagicMock()

    def handle_request(self, request: RequestPayload) -> ResultPayload:
        self.requests.append(request)
        if isinstance(request, GetPathForMacroRequest):
            path = Path(self.folder)
            return GetPathForMacroResultSuccess(result_details="ok", resolved_path=path, absolute_path=path)
        if isinstance(request, WriteFileRequest):
            return WriteFileResultSuccess(result_details="ok", final_file_path=self.folder, bytes_written=3)
        if isinstance(request, AttemptMapAbsolutePathToProjectRequest):
            return AttemptMapAbsolutePathToProjectResultSuccess(result_details="ok", mapped_path=None)
        if isinstance(request, GetSituationRequest):
            return GetSituationResultFailure(result_details="no project situations")
        if isinstance(request, GetNextVersionIndexRequest):
            return GetNextVersionIndexResultSuccess(result_details="ok", index=1)
        msg = f"Unexpected {type(request).__name__}"
        raise AssertionError(msg)

    async def ahandle_request(self, request: RequestPayload) -> ResultPayload:
        return self.handle_request(request)

    @property
    def runtime(self) -> WorkflowRuntime:
        return cast("WorkflowRuntime", self)


@pytest.fixture
def bind() -> Iterator[Callable[[_RecordingWorkflow], None]]:
    """Bind the test's context to a stand-in workflow, as node code is bound to its own."""
    tokens = []

    def bind_to(workflow: _RecordingWorkflow) -> None:
        tokens.append(bound_workflow.set(workflow.runtime))

    yield bind_to
    for token in reversed(tokens):
        bound_workflow.reset(token)


def _in_raw_thread[T](work: Callable[[], T]) -> T:
    """Run `work` on a plain thread, which starts with no binding."""
    outcome: dict[str, Any] = {}

    def run() -> None:
        outcome["binding"] = bound_workflow.get()
        outcome["value"] = work()

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    assert outcome["binding"] is None
    return outcome["value"]


def _sequence_macro() -> MacroPath:
    return MacroPath(ParsedMacro("{outputs}/frames/####.png"), {})


class TestAHandleBuiltInBoundCode:
    def test_a_file_resolves_in_the_workflow_it_was_built_in_from_a_raw_thread(
        self, bind: Callable[[_RecordingWorkflow], None]
    ) -> None:
        a = _RecordingWorkflow("/a/out.png")
        bind(a)
        file = File("{outputs}/out.png")

        assert Path(_in_raw_thread(file.resolve)) == Path("/a/out.png")
        assert file.workflow is a.runtime

    def test_a_named_workflow_wins_over_the_binding(self, bind: Callable[[_RecordingWorkflow], None]) -> None:
        bind(_RecordingWorkflow("/a/out.png"))
        b = _RecordingWorkflow("/b/out.png")

        assert Path(File("{outputs}/out.png", workflow=b.runtime).resolve()) == Path("/b/out.png")

    def test_a_destination_writes_in_its_workflow_and_returns_a_file_for_it(
        self, bind: Callable[[_RecordingWorkflow], None]
    ) -> None:
        a = _RecordingWorkflow("/a/out.png")
        bind(a)
        destination = ProjectFileDestination("{outputs}/out.png")

        written = _in_raw_thread(lambda: destination.write_bytes(b"png"))

        assert [type(request) for request in a.requests] == [WriteFileRequest, AttemptMapAbsolutePathToProjectRequest]
        assert written.workflow is a.runtime

    def test_a_destination_from_a_situation_keeps_the_workflow_it_was_built_in(
        self, bind: Callable[[_RecordingWorkflow], None]
    ) -> None:
        a = _RecordingWorkflow("/a/out.png")
        bind(a)

        destination = ProjectFileDestination.from_situation("out.png", "save_node_output")

        assert destination.workflow is a.runtime

    def test_a_destination_from_a_situation_built_in_a_raw_thread_uses_the_workflow_it_names(self) -> None:
        a = _RecordingWorkflow("/a/out.png")

        destination = _in_raw_thread(
            lambda: ProjectFileDestination.from_situation_in_workflow(a.runtime, "out.png", "save_node_output")
        )

        assert destination.workflow is a.runtime
        assert [type(request) for request in a.requests] == [GetSituationRequest]

    def test_directories_resolve_in_the_workflow_they_were_built_in_from_a_raw_thread(
        self, bind: Callable[[_RecordingWorkflow], None]
    ) -> None:
        a = _RecordingWorkflow("/a/renders")
        bind(a)
        directory = Directory("{outputs}/renders")
        destination = DirectoryDestination("{outputs}/renders")

        assert _in_raw_thread(directory.resolve) == Path("/a/renders")
        assert Path(_in_raw_thread(destination.resolve)) == Path("/a/renders")

    def test_sequences_resolve_in_the_workflow_they_were_built_in_from_a_raw_thread(
        self, bind: Callable[[_RecordingWorkflow], None]
    ) -> None:
        a = _RecordingWorkflow("/a/frames/####.png")
        bind(a)
        sequence = FileSequence(_sequence_macro())
        destination = FileSequenceDestination(_sequence_macro())

        entry = _in_raw_thread(lambda: sequence.entry(7))
        entry_destination = _in_raw_thread(lambda: destination.entry(7))

        assert Path(entry.location) == Path("/a/frames/0007.png")
        assert entry.workflow is a.runtime
        assert entry_destination.workflow is a.runtime
        assert sequence.directory.workflow is a.runtime


class TestAHandleBuiltWhereNoneIsBound:
    def test_it_names_no_workflow(self) -> None:
        token = bound_workflow.set(NO_WORKFLOW)
        try:
            file = File("{outputs}/out.png")
        finally:
            bound_workflow.reset(token)

        assert file.workflow is None


_CONNECTED_DESTINATION_RESULTS: dict[type[RequestPayload], Any] = {
    destination_events.GetConnectedFileDestinationRequest: destination_events.GetConnectedFileDestinationResultSuccess,
    destination_events.GetConnectedDirectoryDestinationRequest: (
        destination_events.GetConnectedDirectoryDestinationResultSuccess
    ),
    destination_events.GetConnectedFileSequenceDestinationRequest: (
        destination_events.GetConnectedFileSequenceDestinationResultSuccess
    ),
}


def _saver_node(engine: Engine, workflow: _RecordingWorkflow, *, connected: object | None = None) -> mock.MagicMock:
    """A node in `workflow` whose output parameter has the settings `connected` supplies, or none."""
    node = mock.MagicMock()
    node.name = "Saver"
    node.engine = engine
    node.workflow = workflow.runtime
    node._require_workflow.return_value = workflow.runtime
    node.get_parameter_value.return_value = "out.png"

    def answer(request: RequestPayload) -> ResultPayload:
        return _CONNECTED_DESTINATION_RESULTS[type(request)](destination=connected, result_details="ok")

    node._handle_request_in_workflow.side_effect = answer
    return node


class TestBuildFile:
    def test_it_writes_in_the_nodes_workflow_when_built_on_a_raw_thread(self, engine: Engine) -> None:
        a = _RecordingWorkflow("/a/out.png")
        parameter = ProjectFileParameter(_saver_node(engine, a), "output_file", default_filename="out.png")

        destination = _in_raw_thread(parameter.build_file)

        assert [type(request) for request in a.requests] == [GetSituationRequest]
        assert destination.workflow is a.runtime

    def test_an_upstream_destination_built_on_a_raw_thread_writes_in_the_nodes_workflow(self, engine: Engine) -> None:
        a = _RecordingWorkflow("/a/out.png")
        settings_spec = FileDestination("{outputs}/out.png").to_spec()
        node = _saver_node(engine, a, connected=settings_spec)
        parameter = ProjectFileParameter(node, "output_file", default_filename="out.png")

        destination = _in_raw_thread(parameter.build_file)

        assert destination.workflow is a.runtime


class TestTheOtherBuilders:
    """`build_directory` and `build_sequence` look the situation up and build in the node's workflow."""

    def test_build_directory_builds_in_the_nodes_workflow_on_a_raw_thread(self, engine: Engine) -> None:
        a = _RecordingWorkflow("/a/renders")
        parameter = ProjectDirectoryParameter(_saver_node(engine, a), "output_dir", default_dirname="renders")

        destination = _in_raw_thread(parameter.build_directory)

        assert [type(request) for request in a.requests] == [GetSituationRequest]
        assert Path(destination.resolve()) == Path("/a/renders")

    def test_build_sequence_builds_in_the_nodes_workflow_on_a_raw_thread(self, engine: Engine) -> None:
        a = _RecordingWorkflow("/a/frames/####.png")
        parameter = ProjectFileSequenceParameter(_saver_node(engine, a), "output_frames", default_filename="f.png")

        destination = _in_raw_thread(parameter.build_sequence)

        assert [type(request) for request in a.requests] == [GetSituationRequest, GetNextVersionIndexRequest]
        assert destination.entry(7).workflow is a.runtime
