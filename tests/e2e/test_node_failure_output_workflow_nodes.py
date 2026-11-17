"""Golden tests that pin what a Workflow node reports when its workflow does not run.

A Workflow node imports the workflow it stands for as a subflow and runs it. It fails in two ways: the
import fails, or the imported workflow runs and a node in it fails. Neither is a failure of a node's own code,
so the node raises a ``RuntimeError`` that frames the underlying text, and ``NodeErrorEvent.error`` has no
node-built parts.

Strict mode is switched off for most of them: while a Workflow node runs, it reads the context manager, and
strict mode then appends its own warning to the node's failure text, once for every read. That is a separate
report, and leaving it in would make these goldens depend on how often a Workflow node reads the context
manager. One test leaves it on and pins what it appends.
"""

from __future__ import annotations

import json
import re
from pathlib import PureWindowsPath
from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.common.strict_mode import STRICT_MODE
from griptape_nodes.retained_mode.events.execution_events import NodeProcessOutcome, StartFlowResultFailure
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.utils.version_utils import engine_version
from tests.e2e.node_failure_support import (
    FIXTURES_DIR,
    FlowRun,
    assert_subflow_failures,
    client_exception,
    client_run_failure,
    create_node_in_flow,
    details_dict,
    expected_details,
    new_flow,
    register_fixture_library,
    start_flow,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.node_error_details import NodeErrorDetails

pytestmark = pytest.mark.timeout(300, method="thread")

WORKFLOW_LIBRARY_DIR = FIXTURES_DIR / "workflow_node_library"
HOST_LIBRARY_NAME = "Failing Workflow Host Library"
WORKFLOW_NODE_TYPE = "FailingWorkflow"
FLOW_FAILURE_PREFIX = "Attempted to run flow 'Flow'. Failed due to: "
INNER_FAILURE = (
    "Node 'Failing' execution failed: Attempted to execute node 'Failing'. "
    "Failed with error: The fixture node refused to run"
)
HOST_PREFIX = "<host>/"


@pytest.fixture
def host_directory(engine: Engine, tmp_path: Path, materialize_library: Callable[..., Path]) -> Path:
    """Register a library with one Workflow node, whose workflow runs a node that always fails.

    Returns the directory that holds the library and the workflow file.
    """
    register_fixture_library(
        engine,
        tmp_path / "worker_leaf_library",
        materialize_library,
        library_dir_name="worker_leaf_library",
        node_file_name="worker_leaf_nodes.py",
    )
    register_fixture_library(
        engine,
        tmp_path / "workflow_node_library",
        materialize_library,
        library_dir_name="workflow_node_library",
        node_file_name="workflow_node_nodes.py",
        extra_files=[WORKFLOW_LIBRARY_DIR / "shout_workflow.py"],
    )
    directory = tmp_path / "host_library"
    directory.mkdir()
    (directory / "fail_workflow.py").write_text((WORKFLOW_LIBRARY_DIR / "fail_workflow.py").read_text())
    schema = {
        "name": HOST_LIBRARY_NAME,
        "library_schema_version": "0.11.0",
        "metadata": {
            "author": "Test Fixture",
            "description": "Library holding a Workflow node whose workflow fails in a leaf node",
            "library_version": "0.1.0",
            "engine_version": engine_version,
            "tags": ["test"],
            "dependencies": {"pip_dependencies": []},
        },
        "categories": [
            {"test": {"title": "test", "description": "Test nodes", "color": "border-gray-500", "icon": "Folder"}}
        ],
        "nodes": [],
        "workflow_nodes": [
            {
                "node_type": WORKFLOW_NODE_TYPE,
                "workflow_path": "fail_workflow.py",
                "metadata": {
                    "category": "test",
                    "description": "Runs the failing workflow",
                    "display_name": "Failing Workflow",
                },
            }
        ],
    }
    library_json = directory / "griptape_nodes_library.json"
    library_json.write_text(json.dumps(schema, indent=2))
    result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(result, RegisterLibraryFromFileResultSuccess), result
    return directory


async def _run_outer(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> FlowRun:
    """Run a flow holding the Workflow node ``Outer``."""
    flow_name = new_flow(engine, "workflow_node_failure_output")
    create_node_in_flow(engine, WORKFLOW_NODE_TYPE, "Outer", flow_name, library_name=HOST_LIBRARY_NAME)
    return await start_flow(engine, monkeypatch, flow_name)


class TestWorkflowNodeWhoseInnerNodeFails:
    """Golden case 13: the workflow imported and ran, and a node in it failed.

    The inner node reports its own failure first, under its own name, with the type it raised. The Workflow node
    then reports ``did not finish`` with the inner text after it, as a ``builtins.RuntimeError`` whose message
    is its whole text. The run's recorded message is the node's text with no group-style framing.
    """

    WORKFLOW_NODE_WORDS = (
        "Attempted to run the workflow behind node 'Outer'. "
        f"Failed because the workflow did not finish: {INNER_FAILURE}"
    )
    OUTER_MESSAGE = (
        f"Node 'Outer' execution failed: Attempted to execute node 'Outer'. Failed with error: {WORKFLOW_NODE_WORDS}"
    )

    @pytest.mark.usefixtures("host_directory", "strict_mode_off")
    @pytest.mark.asyncio
    async def test_start_flow(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await _run_outer(engine, monkeypatch)

        assert isinstance(run.result, StartFlowResultFailure)
        assert run.run_message == self.OUTER_MESSAGE
        assert run.result_details == FLOW_FAILURE_PREFIX + self.OUTER_MESSAGE
        assert run.validation_exception_messages == [self.OUTER_MESSAGE]
        assert [(event.node_name, event.error_message) for event in run.recorded.node_errors] == [
            ("Failing", INNER_FAILURE),
            ("Outer", self.OUTER_MESSAGE),
        ]
        assert [details_dict(event.error) for event in run.recorded.node_errors] == [
            expected_details("The fixture node refused to run", "griptape_nodes.exe_types.node_error.NodeError"),
            expected_details(self.WORKFLOW_NODE_WORDS, "builtins.RuntimeError"),
        ]
        assert [
            (finish.node_name, finish.flow_name, finish.outcome, finish.error_message)
            for finish in run.recorded.node_finishes
            if finish.error_message is not None
        ] == [
            ("Failing", "ControlFlow_1", NodeProcessOutcome.FAILED, INNER_FAILURE),
            ("Outer", "Flow", NodeProcessOutcome.FAILED, self.OUTER_MESSAGE),
        ]
        assert [details_dict(finish.error) for finish in run.recorded.failed_finishes()] == [
            expected_details("The fixture node refused to run", "griptape_nodes.exe_types.node_error.NodeError"),
            expected_details(self.WORKFLOW_NODE_WORDS, "builtins.RuntimeError"),
        ]
        run.recorded.assert_finish_errors_match_node_errors()
        assert run.client == client_run_failure(
            FLOW_FAILURE_PREFIX + self.OUTER_MESSAGE, [client_exception(self.OUTER_MESSAGE)]
        )
        assert_subflow_failures(run.subflow_runs, [("ControlFlow_1", INNER_FAILURE)])


class TestWorkflowNodeWithStrictModeOn:
    """Golden case 13 with strict mode on: a Workflow node runs outside any node's run, so strict mode adds nothing."""

    @pytest.mark.usefixtures("host_directory")
    @pytest.mark.asyncio
    async def test_start_flow(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(STRICT_MODE, "_enabled", True)

        run = await _run_outer(engine, monkeypatch)

        golden = TestWorkflowNodeWhoseInnerNodeFails.OUTER_MESSAGE
        inner_details = expected_details(
            "The fixture node refused to run", "griptape_nodes.exe_types.node_error.NodeError"
        )
        outer_details = expected_details(
            TestWorkflowNodeWhoseInnerNodeFails.WORKFLOW_NODE_WORDS, "builtins.RuntimeError"
        )
        assert isinstance(run.result, StartFlowResultFailure)
        assert run.run_message == golden
        assert run.result_details == FLOW_FAILURE_PREFIX + golden
        assert run.validation_exception_messages == [golden]
        assert [event.node_name for event in run.recorded.node_errors] == ["Failing", "Outer"]
        inner_error, outer_error = run.recorded.node_errors
        assert inner_error.error_message == INNER_FAILURE
        assert outer_error.error_message == golden
        assert [details_dict(event.error) for event in run.recorded.node_errors] == [inner_details, outer_details]
        inner_finish, outer_finish = run.recorded.failed_finishes()
        assert inner_finish.error_message == INNER_FAILURE
        assert outer_finish.error_message == golden
        assert [details_dict(finish.error) for finish in run.recorded.failed_finishes()] == [
            inner_details,
            outer_details,
        ]
        run.recorded.assert_finish_errors_match_node_errors()
        assert run.client == client_run_failure(FLOW_FAILURE_PREFIX + golden, [client_exception(golden)])


class TestWorkflowNodeWhoseImportFails:
    """Golden case 13: the workflow file is gone, so importing it as a subflow fails.

    The text names the file, so the directory the test made is masked as ``<host>/``, whichever way the
    platform spells it.
    """

    @pytest.mark.usefixtures("strict_mode_off")
    @pytest.mark.asyncio
    async def test_start_flow(self, engine: Engine, monkeypatch: pytest.MonkeyPatch, host_directory: Path) -> None:
        (host_directory / "fail_workflow.py").unlink()

        run = await _run_outer(engine, monkeypatch)

        masked = _host_directory_mask(host_directory)
        workflow_file = f"{HOST_PREFIX}fail_workflow.py"
        import_words = (
            f"Attempted to import workflow '{HOST_PREFIX}fail_workflow' as referenced sub flow. "
            f"Failed because workflow execution failed: Failed to run workflow on path '{workflow_file}'. "
            f"Exception: [Errno 2] No such file or directory: '{workflow_file}'"
        )
        workflow_node_words = (
            f"Attempted to load the workflow at '{workflow_file}' for node 'Outer'. "
            f"Failed because the workflow could not be loaded: {import_words}"
        )
        outer_message = (
            f"Node 'Outer' execution failed: Attempted to execute node 'Outer'. "
            f"Failed with error: {workflow_node_words}"
        )
        expected_error = expected_details(workflow_node_words, "builtins.RuntimeError")
        assert isinstance(run.result, StartFlowResultFailure)
        assert masked(run.run_message or "") == outer_message
        assert masked(run.result_details) == FLOW_FAILURE_PREFIX + outer_message
        assert [masked(message) for message in run.validation_exception_messages] == [outer_message]
        assert [(event.node_name, masked(event.error_message)) for event in run.recorded.node_errors] == [
            ("Outer", outer_message)
        ]
        assert [masked_details(event.error, masked) for event in run.recorded.node_errors] == [expected_error]
        [finish] = run.recorded.node_finishes
        assert (finish.node_name, finish.flow_name, finish.outcome) == ("Outer", "Flow", NodeProcessOutcome.FAILED)
        assert masked(finish.error_message or "") == outer_message
        assert masked_details(finish.error, masked) == expected_error
        run.recorded.assert_finish_errors_match_node_errors()
        client = run.client
        client["result_details"]["result_details"][0]["message"] = masked(
            client["result_details"]["result_details"][0]["message"]
        )
        client["validation_exceptions"][0]["message"] = masked(client["validation_exceptions"][0]["message"])
        assert client == client_run_failure(FLOW_FAILURE_PREFIX + outer_message, [client_exception(outer_message)])


class _WindowsDirectory(PureWindowsPath):
    """A Windows path that stands for itself when resolved, so the mask can be exercised on any platform."""

    def resolve(self, strict: bool = False) -> _WindowsDirectory:  # noqa: ARG002, FBT001, FBT002
        return self


class TestHostDirectoryMask:
    @pytest.fixture
    def directory(self) -> _WindowsDirectory:
        return _WindowsDirectory("C:\\Users\\artist\\host_library")

    @pytest.mark.parametrize(
        "text",
        [
            "C:\\Users\\artist\\host_library\\fail_workflow",
            "C:\\\\Users\\\\artist\\\\host_library\\\\fail_workflow",
            "C:/Users/artist/host_library/fail_workflow",
        ],
    )
    def test_masks_every_spelling_of_the_directory(self, directory: _WindowsDirectory, text: str) -> None:
        mask = _host_directory_mask(directory)  # type: ignore[arg-type]

        assert mask(f"cannot import {text}") == "cannot import <host>/fail_workflow"


def masked_details(details: NodeErrorDetails | None, masked: Callable[[str], str]) -> dict[str, Any] | None:
    """``details`` as a dict, with the host directory masked out of its message."""
    as_dict = details_dict(details)
    if as_dict is None:
        return None
    as_dict["message"] = masked(as_dict["message"])
    return as_dict


def _host_directory_mask(directory: Path) -> Callable[[str], str]:
    """A function that replaces ``directory`` and the separator after it with ``<host>/``, in any spelling.

    The spellings are the directory as given, as resolved (a Windows temporary directory can be an 8.3 short
    path), and each of those with forward slashes (a workflow registry key is built that way) and as ``repr``
    shows a path, with its backslashes doubled. The separator after it is a slash, a backslash, or a doubled
    backslash.
    """
    spellings: set[str] = set()
    for path in (directory, directory.resolve()):
        for candidate in (str(path), path.as_posix()):
            spellings.add(candidate)
            spellings.add(candidate.replace("\\", "\\\\"))
    alternatives = "|".join(re.escape(spelling) for spelling in sorted(spellings, key=len, reverse=True))
    pattern = re.compile(rf"(?:{alternatives})(?:\\\\|[\\/])")

    def mask(text: str) -> str:
        return pattern.sub(HOST_PREFIX, text)

    return mask
