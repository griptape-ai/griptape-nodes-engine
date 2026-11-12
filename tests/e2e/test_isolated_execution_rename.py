"""A group saved with the old "Private Execution" setting opens with "Isolated Execution".

The setting is saved as its plain value, and `Options` replaces a value it does not offer with its
first choice, Local Execution. Without the mapping in `SubflowNodeGroup.set_parameter_value`, a
renamed choice would quietly move every saved group out of isolation.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.node_types import ISOLATED_EXECUTION, LEGACY_PRIVATE_EXECUTION, LOCAL_EXECUTION
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "subflow_library"
LIBRARY_NAME = "Isolated Execution Rename Library"


def _create_group(engine: Engine, tmp_path: Path, materialize_library: Callable[..., Path]) -> str:
    library_json = materialize_library(
        tmp_path / "library",
        template=FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json",
        node_file=FIXTURE_LIBRARY_DIR / "subflow_echo_node.py",
        name=LIBRARY_NAME,
    )
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result
    engine.context_manager.push_workflow(workflow_name="isolated_execution_rename_wf")
    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="Flow", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    with engine.context_manager.flow(flow_result.flow_name):
        group_result = engine.handle_request(
            CreateNodeRequest(node_type="SubflowGroupNode", specific_library_name=LIBRARY_NAME, node_name="Group")
        )
    assert isinstance(group_result, CreateNodeResultSuccess), group_result
    return group_result.node_name


@pytest.mark.parametrize("initial_setup", [True, False], ids=["loading a saved workflow", "setting it by hand"])
def test_the_old_name_is_read_as_isolated_execution(
    tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path], *, initial_setup: bool
) -> None:
    """A saved workflow sets the value with initial_setup, which skips the node's value hooks."""
    group_name = _create_group(engine, tmp_path, materialize_library)

    engine.handle_request(
        SetParameterValueRequest(
            parameter_name="execution_environment",
            node_name=group_name,
            value=LEGACY_PRIVATE_EXECUTION,
            initial_setup=initial_setup,
        )
    )

    group = engine.node_manager.get_node_by_name(group_name)
    assert group.get_parameter_value("execution_environment") == ISOLATED_EXECUTION


def test_the_choices_offer_only_the_new_name(
    tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]
) -> None:
    """The editor offers the new name only; the old one is read, never shown."""
    group_name = _create_group(engine, tmp_path, materialize_library)
    group = engine.node_manager.get_node_by_name(group_name)

    parameter = group.get_parameter_by_name("execution_environment")
    assert parameter is not None
    choices = parameter.ui_options["simple_dropdown"]

    assert choices[:2] == [LOCAL_EXECUTION, ISOLATED_EXECUTION]
    assert LEGACY_PRIVATE_EXECUTION not in choices
