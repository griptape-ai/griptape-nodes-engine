from typing import Any
from unittest.mock import ANY

import pytest  # type: ignore[reportMissingImports]

from griptape_nodes.retained_mode.events.base_events import EventResultSuccess
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    CreateNodeResultSuccess,
    GetAllNodeInfoRequest,
    GetAllNodeInfoResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

pytestmark = pytest.mark.xfail(strict=True, reason="Drifted due to not running on CI - see #5237")


class TestNodeEvents:
    @pytest.fixture
    def create_node_result(self) -> Any:
        request = CreateNodeRequest(node_type="RunAgentNode", override_parent_flow_name="canvas")
        result = GriptapeNodes.handle_request(request)

        return result

    def test_GetAllNodeInfoResult(self, create_node_result: CreateNodeResultSuccess) -> None:
        request = GetAllNodeInfoRequest(node_name=create_node_result.node_name)
        result = GriptapeNodes.handle_request(request)

        assert isinstance(result, GetAllNodeInfoResultSuccess)

        assert EventResultSuccess(request=request, result=result).dict() == {
            "request": {"request_id": None, "node_name": "RunAgentNode_1"},
            "result": {
                "metadata": {
                    "library_node_metadata": {
                        "category": "Agent",
                        "description": "Griptape Agent that can execute prompts and use tools",
                        "display_name": "Run Agent",
                    },
                    "library": "Griptape Nodes Library",
                    "node_type": "RunAgentNode",
                },
                "node_resolution_state": "UNRESOLVED",
                "connections": {"incoming_connections": [], "outgoing_connections": []},
                "parameter_name_to_info": {
                    "agent": {
                        "details": {
                            "element_id": ANY,
                            "input_types": ["Agent"],
                            "output_type": "Agent",
                            "type": "Agent",
                            "default_value": None,
                            "tooltip": "",
                            "tooltip_as_input": None,
                            "tooltip_as_property": None,
                            "tooltip_as_output": None,
                            "mode_allowed_input": True,
                            "mode_allowed_property": True,
                            "mode_allowed_output": True,
                            "is_user_defined": False,
                            "ui_options": None,
                        },
                        "value": {"data_type": "<class 'NoneType'>", "value": None},
                    },
                    "prompt_driver": {
                        "details": {
                            "element_id": ANY,
                            "input_types": ["Prompt Driver"],
                            "output_type": "Prompt Driver",
                            "type": "Prompt Driver",
                            "default_value": None,
                            "tooltip": "",
                            "tooltip_as_input": None,
                            "tooltip_as_property": None,
                            "tooltip_as_output": None,
                            "mode_allowed_input": True,
                            "mode_allowed_property": True,
                            "mode_allowed_output": True,
                            "is_user_defined": False,
                            "ui_options": None,
                        },
                        "value": {"data_type": "<class 'NoneType'>", "value": None},
                    },
                    "prompt_model": {
                        "details": {
                            "element_id": ANY,
                            "input_types": ["str"],
                            "output_type": "str",
                            "type": "str",
                            "default_value": "gpt-4o",
                            "tooltip": "",
                            "tooltip_as_input": None,
                            "tooltip_as_property": None,
                            "tooltip_as_output": None,
                            "mode_allowed_input": True,
                            "mode_allowed_property": True,
                            "mode_allowed_output": True,
                            "is_user_defined": False,
                            "ui_options": None,
                        },
                        "value": {"data_type": "str", "value": "gpt-4o"},
                    },
                    "prompt": {
                        "details": {
                            "element_id": ANY,
                            "input_types": ["str"],
                            "output_type": "str",
                            "type": "str",
                            "default_value": "",
                            "tooltip": "",
                            "tooltip_as_input": None,
                            "tooltip_as_property": None,
                            "tooltip_as_output": None,
                            "mode_allowed_input": True,
                            "mode_allowed_property": True,
                            "mode_allowed_output": True,
                            "is_user_defined": False,
                            "ui_options": {
                                "string_type_options": {"multiline": True, "markdown": None, "placeholder_text": None},
                                "boolean_type_options": None,
                                "number_type_options": None,
                                "simple_dropdown_options": None,
                                "fancy_dropdown_options": None,
                                "image_type_options": None,
                                "video_type_options": None,
                                "audio_type_options": None,
                                "property_array_type_options": None,
                                "list_container_options": None,
                                "display": True,
                            },
                        },
                        "value": {"data_type": "str", "value": ""},
                    },
                    "tool": {
                        "details": {
                            "element_id": ANY,
                            "input_types": ["BaseTool"],
                            "output_type": "BaseTool",
                            "type": "BaseTool",
                            "default_value": None,
                            "tooltip": "",
                            "tooltip_as_input": None,
                            "tooltip_as_property": None,
                            "tooltip_as_output": None,
                            "mode_allowed_input": True,
                            "mode_allowed_property": True,
                            "mode_allowed_output": True,
                            "is_user_defined": False,
                            "ui_options": None,
                        },
                        "value": {"data_type": "<class 'NoneType'>", "value": None},
                    },
                    "tool_list": {
                        "details": {
                            "element_id": ANY,
                            "input_types": ["list[BaseTool]"],
                            "output_type": "list[BaseTool]",
                            "type": "list[BaseTool]",
                            "default_value": None,
                            "tooltip": "",
                            "tooltip_as_input": None,
                            "tooltip_as_property": None,
                            "tooltip_as_output": None,
                            "mode_allowed_input": True,
                            "mode_allowed_property": True,
                            "mode_allowed_output": True,
                            "is_user_defined": False,
                            "ui_options": None,
                        },
                        "value": {"data_type": "<class 'NoneType'>", "value": None},
                    },
                    "ruleset": {
                        "details": {
                            "element_id": ANY,
                            "input_types": ["Ruleset"],
                            "output_type": "Ruleset",
                            "type": "Ruleset",
                            "default_value": None,
                            "tooltip": "",
                            "tooltip_as_input": None,
                            "tooltip_as_property": None,
                            "tooltip_as_output": None,
                            "mode_allowed_input": True,
                            "mode_allowed_property": True,
                            "mode_allowed_output": True,
                            "is_user_defined": False,
                            "ui_options": None,
                        },
                        "value": {"data_type": "<class 'NoneType'>", "value": None},
                    },
                    "output": {
                        "details": {
                            "element_id": ANY,
                            "output_type": "str",
                            "type": "str",
                            "default_value": "",
                            "tooltip": "What the agent said.",
                            "tooltip_as_input": None,
                            "tooltip_as_property": None,
                            "tooltip_as_output": None,
                            "mode_allowed_input": False,
                            "mode_allowed_property": False,
                            "mode_allowed_output": True,
                            "is_user_defined": False,
                            "ui_options": {
                                "string_type_options": {
                                    "multiline": True,
                                    "markdown": None,
                                    "placeholder_text": "The agent response",
                                },
                                "boolean_type_options": None,
                                "number_type_options": None,
                                "simple_dropdown_options": None,
                                "fancy_dropdown_options": None,
                                "image_type_options": None,
                                "video_type_options": None,
                                "audio_type_options": None,
                                "property_array_type_options": None,
                                "list_container_options": None,
                                "display": True,
                            },
                        },
                        "value": {"data_type": "str", "value": ""},
                    },
                },
                "root_node_element": {
                    "element_id": ANY,
                    "element_type": "BaseNodeElement",
                    "children": [
                        {"element_id": ANY, "element_type": "Parameter", "children": []},
                        {
                            "element_id": ANY,
                            "element_type": "ParameterGroup",
                            "name": "Agent Config",
                            "children": [
                                {
                                    "element_id": ANY,
                                    "element_type": "Parameter",
                                    "children": [],
                                },
                                {
                                    "element_id": ANY,
                                    "element_type": "Parameter",
                                    "children": [],
                                },
                                {
                                    "element_id": ANY,
                                    "element_type": "Parameter",
                                    "children": [],
                                },
                            ],
                        },
                        {
                            "element_id": ANY,
                            "element_type": "ParameterGroup",
                            "name": "Agent Tools",
                            "children": [
                                {
                                    "element_id": ANY,
                                    "element_type": "Parameter",
                                    "children": [],
                                },
                                {
                                    "element_id": ANY,
                                    "element_type": "Parameter",
                                    "children": [],
                                },
                                {
                                    "element_id": ANY,
                                    "element_type": "Parameter",
                                    "children": [],
                                },
                                {
                                    "element_id": ANY,
                                    "element_type": "Parameter",
                                    "children": [],
                                },
                            ],
                        },
                    ],
                },
            },
            "retained_mode": None,
            "event_type": "EventResultSuccess",
            "request_type": "GetAllNodeInfoRequest",
            "result_type": "GetAllNodeInfoResultSuccess",
        }
