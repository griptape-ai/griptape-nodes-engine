"""Loading a workflow saved before trait state was carried in its own right.

Such a file holds a run-time dropdown update in the parameter's ``ui_options`` under
``simple_dropdown``, because that was the only field a save carried. The engine now keeps
those choices on the trait, so the load has to adopt the old mirror. Left unread, the trait
falls back to whatever the node's ``__init__`` builds and the Options converter rewrites the
saved selection to the first of those.
"""

from collections.abc import Generator

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.node_library.library_registry import (
    LibraryMetadata,
    LibraryRegistry,
    LibrarySchema,
    NodeMetadata,
)
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.context_events import (
    EnsureWorkflowAndFlowRequest,
    EnsureWorkflowAndFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.events.parameter_events import (
    AlterParameterDetailsRequest,
    AlterParameterDetailsResultSuccess,
    SetParameterValueRequest,
)
from griptape_nodes.traits.options import Options

_LIBRARY_NAME = "legacy-ui-options-test-library"

# What the old engine wrote for a dropdown its node filled in at run time: the trait's
# rendered options, merged into the parameter's own and saved as one dict.
_LEGACY_UI_OPTIONS = {
    "simple_dropdown": ["base-1", "runtime-a", "runtime-b"],
    "show_search": True,
    "search_filter": "",
    "hide": True,
}


class _ModelNode(BaseNode):
    """Declares a dropdown knowing only its built-in choice, as a driver node does."""

    def __init__(self, name: str = "picker", metadata: dict | None = None) -> None:
        super().__init__(name=name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="model",
                type="str",
                tooltip="t",
                default_value="base-1",
                allowed_modes={ParameterMode.PROPERTY},
                traits={Options(choices=["base-1"])},
            )
        )

    def process(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _registered_node_type() -> Generator[None, None, None]:
    LibraryRegistry._clear()
    schema = LibrarySchema(
        name=_LIBRARY_NAME,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(author="t", description="d", library_version="1.0.0", engine_version="1.0.0", tags=[]),
        categories=[],
        nodes=[],
    )
    library = LibraryRegistry.generate_new_library(library_data=schema)
    library.register_new_node_type(_ModelNode, NodeMetadata(category="t", description="d", display_name="Model Node"))
    yield
    LibraryRegistry._clear()


def _loaded_node(engine: Engine) -> _ModelNode:
    """Replay the commands a legacy file holds for this node, in the order a load runs them."""
    context = engine.handle_request(
        EnsureWorkflowAndFlowRequest(workflow_name="legacy_ui_options_test", display_name="legacy_ui_options_test")
    )
    assert isinstance(context, EnsureWorkflowAndFlowResultSuccess)
    created = engine.handle_request(
        CreateNodeRequest(node_type=_ModelNode.__name__, specific_library_name=_LIBRARY_NAME, node_name="picker")
    )
    assert isinstance(created, CreateNodeResultSuccess)
    node = engine.object_manager.get_object_by_name(created.node_name)
    assert isinstance(node, _ModelNode)

    altered = engine.handle_request(
        AlterParameterDetailsRequest(
            node_name=node.name,
            parameter_name="model",
            ui_options=_LEGACY_UI_OPTIONS,
            initial_setup=True,
        )
    )
    assert isinstance(altered, AlterParameterDetailsResultSuccess)
    engine.handle_request(
        SetParameterValueRequest(
            node_name=node.name,
            parameter_name="model",
            value="runtime-b",
            initial_setup=True,
        )
    )
    return node


class TestALegacyDropdownSurvivesTheLoad:
    def test_the_saved_selection_is_kept(self, engine: Engine) -> None:
        # The load runs converters but skips after_value_set, so the node never gets the hook
        # it would use to refill the dropdown. The file has to carry the choices through.
        node = _loaded_node(engine)

        assert node.get_parameter_value("model") == "runtime-b"

    def test_the_choices_are_reported_to_the_editor(self, engine: Engine) -> None:
        node = _loaded_node(engine)
        parameter = node.get_parameter_by_name("model")
        assert parameter is not None

        assert parameter.ui_options["simple_dropdown"] == ["base-1", "runtime-a", "runtime-b"]

    def test_the_choices_are_carried_as_trait_state_from_now_on(self, engine: Engine) -> None:
        node = _loaded_node(engine)
        parameter = node.get_parameter_by_name("model")
        assert parameter is not None

        state = parameter.trait_states()[0]["trait_state"]

        assert state["choices"] == ["base-1", "runtime-a", "runtime-b"]

    def test_the_authored_options_beside_them_are_kept(self, engine: Engine) -> None:
        node = _loaded_node(engine)
        parameter = node.get_parameter_by_name("model")
        assert parameter is not None

        assert parameter.authored_ui_options() == {"hide": True}
