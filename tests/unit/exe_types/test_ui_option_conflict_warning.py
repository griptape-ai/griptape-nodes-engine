import logging

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterGroup
from tests.unit.exe_types.mocks import MockNode


def _conflicting_parameter() -> Parameter:
    return Parameter(
        name="input_image",
        type="str",
        hide_property=True,
        ui_options={"hide_property": False},
    )


class TestUIOptionConflictWarning:
    def test_warning_names_node_and_library(self, caplog: pytest.LogCaptureFixture) -> None:
        node = MockNode(name="Paint Mask_1", metadata={"library": "Example Library", "node_type": "PaintMask"})

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            node.add_parameter(_conflicting_parameter())

        messages = [record.getMessage() for record in caplog.records if "Conflicting values" in record.getMessage()]
        assert len(messages) == 1
        assert "Node 'Paint Mask_1' (PaintMask from library 'Example Library')" in messages[0]
        assert "Parameter 'input_image'" in messages[0]
        assert "contact the author of library 'Example Library'" in messages[0]

    def test_warning_waits_until_element_joins_a_node(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            parameter = _conflicting_parameter()

        assert not any("Conflicting values" in record.getMessage() for record in caplog.records)

        node = MockNode(name="node_a")
        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            node.add_parameter(parameter)

        messages = [record.getMessage() for record in caplog.records if "Conflicting values" in record.getMessage()]
        assert len(messages) == 1
        assert "Node 'node_a' (MockNode)" in messages[0]

    def test_warning_for_parameter_inside_group(self, caplog: pytest.LogCaptureFixture) -> None:
        with ParameterGroup(name="settings") as group:
            _conflicting_parameter()

        node = MockNode(name="node_b", metadata={"library": "Example Library"})
        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            node.add_node_element(group)

        messages = [record.getMessage() for record in caplog.records if "Conflicting values" in record.getMessage()]
        assert len(messages) == 1
        assert "Node 'node_b'" in messages[0]
