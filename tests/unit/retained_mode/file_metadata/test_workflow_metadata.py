"""Unit tests for workflow_metadata.py provenance collection functions."""

import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import Mock, patch

import pytest

from griptape_nodes.exe_types.core_types import ParameterMode
from griptape_nodes.retained_mode.file_metadata.workflow_metadata import (
    _collect_workflow_info,
    _parameter_values_of,
    collect_sidecar_provenance,
    collect_workflow_metadata,
)


def _make_param(
    name: str, *, exclude_from_metadata: bool = False, value: Any = None, modes: set | None = None
) -> tuple[Mock, Any]:
    param = Mock()
    param.name = name
    param.exclude_from_metadata = exclude_from_metadata
    param.allowed_modes = modes if modes is not None else {ParameterMode.INPUT, ParameterMode.PROPERTY}
    return param, value


class TestCollectWorkflowInfo:
    def test_returns_name_when_registry_lookup_fails(self) -> None:
        engine = Mock()
        engine.workflow_registry.get_workflow_by_name.side_effect = KeyError("not found")

        result = _collect_workflow_info(engine, "my_workflow")

        assert result == {"name": "my_workflow"}

    def test_includes_dates_and_version_when_present(self) -> None:
        workflow = Mock()
        workflow.metadata.creation_date = datetime(2026, 1, 1, tzinfo=UTC)
        workflow.metadata.last_modified_date = datetime(2026, 6, 1, tzinfo=UTC)
        workflow.metadata.engine_version_created_with = "1.2.3"
        workflow.metadata.description = None

        engine = Mock()
        engine.workflow_registry.get_workflow_by_name.return_value = workflow

        result = _collect_workflow_info(engine, "my_workflow")

        assert result["name"] == "my_workflow"
        assert "created" in result
        assert "modified" in result
        assert result["engine_version"] == "1.2.3"
        assert "description" not in result

    def test_omits_none_metadata_fields(self) -> None:
        workflow = Mock()
        workflow.metadata.creation_date = None
        workflow.metadata.last_modified_date = None
        workflow.metadata.engine_version_created_with = None
        workflow.metadata.description = None

        engine = Mock()
        engine.workflow_registry.get_workflow_by_name.return_value = workflow

        result = _collect_workflow_info(engine, "my_workflow")

        assert result == {"name": "my_workflow"}


class TestParameterValuesOf:
    def test_returns_none_when_there_is_no_node(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            result = _parameter_values_of("missing_node", None)

        assert result is None
        assert "missing_node" in caplog.text

    def test_collects_non_excluded_parameters(self) -> None:
        p1, _ = _make_param("prompt", value=None)
        p2, _ = _make_param("model", value=None)

        values = {"prompt": "hello", "model": "gpt-4"}
        node = Mock()
        # A Mock answers truthy to everything; these values are not parked keys.
        node.local_objects.contains_a_parked_object.return_value = False
        node.parameters = [p1, p2]
        node._get_raw_parameter_value.side_effect = values.get

        result = _parameter_values_of("my_node", node)

        assert result is not None
        assert result.values == {"prompt": "hello", "model": "gpt-4"}
        assert result.omitted == []

    def test_excludes_metadata_excluded_parameters_from_values(self) -> None:
        pub, _ = _make_param("prompt", value=None)
        priv, _ = _make_param("password", exclude_from_metadata=True, value=None)

        values = {"prompt": "hello", "password": "secret"}
        node = Mock()
        # A Mock answers truthy to everything; these values are not parked keys.
        node.local_objects.contains_a_parked_object.return_value = False
        node.parameters = [pub, priv]
        node._get_raw_parameter_value.side_effect = values.get

        result = _parameter_values_of("my_node", node)

        assert result is not None
        assert "password" not in result.values
        assert "prompt" in result.values
        assert result.omitted == ["password"]

    def test_skips_none_values(self) -> None:
        p, _ = _make_param("optional_param", value=None)

        node = Mock()
        # A Mock answers truthy to everything; these values are not parked keys.
        node.local_objects.contains_a_parked_object.return_value = False
        node.parameters = [p]
        node._get_raw_parameter_value.return_value = None

        result = _parameter_values_of("my_node", node)

        assert result is not None
        assert result.values == {}
        assert result.omitted == []

    def test_skips_output_only_parameters(self) -> None:
        p, _ = _make_param("output_image", value=None, modes={ParameterMode.OUTPUT})

        node = Mock()
        # A Mock answers truthy to everything; these values are not parked keys.
        node.local_objects.contains_a_parked_object.return_value = False
        node.parameters = [p]
        node._get_raw_parameter_value.return_value = "some_value"

        result = _parameter_values_of("my_node", node)

        assert result is not None
        assert result.values == {}

    def test_multiple_excluded_params_all_listed_in_omitted(self) -> None:
        p1, _ = _make_param("api_password", exclude_from_metadata=True, value=None)
        p2, _ = _make_param("db_password", exclude_from_metadata=True, value=None)
        p3, _ = _make_param("prompt", value=None)

        values = {"api_password": "s3cr3t", "db_password": "hunter2", "prompt": "hello"}
        node = Mock()
        # A Mock answers truthy to everything; these values are not parked keys.
        node.local_objects.contains_a_parked_object.return_value = False
        node.parameters = [p1, p2, p3]
        node._get_raw_parameter_value.side_effect = values.get

        result = _parameter_values_of("my_node", node)

        assert result is not None
        assert set(result.omitted) == {"api_password", "db_password"}
        assert result.values == {"prompt": "hello"}


def _make_workflow(
    engine: Mock,
    *,
    workflow_name: str = "test_workflow",
    has_flow: bool = True,
    flow_name: str = "ControlFlow_1",
    resolving_nodes: list[str] | None = None,
) -> Mock:
    """Build a mock workflow runtime, and answer the engine's per-workflow lookups for it."""
    workflow = Mock()
    workflow.workflow_id = "wf-1"
    workflow.context.has_current_flow.return_value = has_flow
    flow = Mock()
    flow.name = flow_name
    workflow.context.get_current_flow.return_value = flow
    workflow.objects.get_as_type.return_value = None
    engine.flow_manager.flow_state_in.return_value = (None, resolving_nodes or [], None)
    engine.workflow_registry.get.return_value.name = workflow_name
    return workflow


class TestCollectSidecarProvenance:
    def test_with_no_workflow_records_nothing(self) -> None:
        engine = Mock()

        assert collect_sidecar_provenance(engine, None) == {}

    def test_reads_the_given_workflow(self) -> None:
        engine = Mock()
        workflow = _make_workflow(engine, workflow_name="given", flow_name="GivenFlow")

        result = collect_sidecar_provenance(engine, workflow)

        assert result["workflow"]["name"] == "given"
        assert result["flow"]["name"] == "GivenFlow"
        engine.workflow_registry.get.assert_called_with("wf-1")

    def test_flow_block_uses_first_resolving_node_as_node_name(self) -> None:
        engine = Mock()
        workflow = _make_workflow(engine, resolving_nodes=["NodeA", "NodeB"])

        result = collect_sidecar_provenance(engine, workflow)

        assert result["flow"]["node_name"] == "NodeA"
        assert "resolving_nodes" not in result["flow"]

    def test_excluded_params_in_parameters_omitted_not_parameters(self) -> None:
        priv, _ = _make_param("password", exclude_from_metadata=True, value=None)
        pub, _ = _make_param("prompt", value=None)

        param_values = {"password": "s3cr3t", "prompt": "hi"}
        node = Mock()
        # A Mock answers truthy to everything; these values are not parked keys.
        node.local_objects.contains_a_parked_object.return_value = False
        node.parameters = [priv, pub]
        node._get_raw_parameter_value.side_effect = param_values.get

        engine = Mock()
        workflow = _make_workflow(engine, resolving_nodes=["MyNode"])
        workflow.objects.get_as_type.return_value = node

        result = collect_sidecar_provenance(engine, workflow)

        assert result["parameters"] == {"prompt": "hi"}
        assert result["parameters_omitted"] == ["password"]

    def test_parameters_omitted_absent_when_no_excluded_params(self) -> None:
        pub, _ = _make_param("prompt", value=None)

        node = Mock()
        # A Mock answers truthy to everything; these values are not parked keys.
        node.local_objects.contains_a_parked_object.return_value = False
        node.parameters = [pub]
        node._get_raw_parameter_value.return_value = "hello"

        engine = Mock()
        workflow = _make_workflow(engine, resolving_nodes=["MyNode"])
        workflow.objects.get_as_type.return_value = node

        result = collect_sidecar_provenance(engine, workflow)

        assert "parameters_omitted" not in result
        assert "prompt" in result["parameters"]

    def test_no_flow_block_when_no_flow_context(self) -> None:
        engine = Mock()
        workflow = _make_workflow(engine, has_flow=False)

        result = collect_sidecar_provenance(engine, workflow)

        assert "workflow" in result
        assert "flow" not in result
        assert "parameters" not in result


class TestCollectWorkflowMetadata:
    def test_returns_only_saved_at_with_no_workflow(self) -> None:
        result = collect_workflow_metadata(Mock(), None)

        assert list(result) == ["gtn_saved_at"]

    def test_reads_the_given_workflow(self) -> None:
        engine = Mock()
        workflow = _make_workflow(engine, workflow_name="given", flow_name="GivenFlow", resolving_nodes=["MyNode"])

        with patch(
            "griptape_nodes.retained_mode.file_metadata.workflow_metadata._serialize_flow",
            return_value=None,
        ) as serialize:
            result = collect_workflow_metadata(engine, workflow)

        assert result["gtn_workflow_name"] == "given"
        assert result["gtn_flow_name"] == "GivenFlow"
        assert result["gtn_node_name"] == "MyNode"
        serialize.assert_called_once_with(workflow, "GivenFlow")

    def test_excluded_params_excluded_from_png_metadata(self) -> None:
        priv, _ = _make_param("password", exclude_from_metadata=True, value=None)
        pub, _ = _make_param("prompt", value=None)

        param_values = {"password": "s3cr3t", "prompt": "hello"}
        node = Mock()
        # A Mock answers truthy to everything; these values are not parked keys.
        node.local_objects.contains_a_parked_object.return_value = False
        node.parameters = [priv, pub]
        node._get_raw_parameter_value.side_effect = param_values.get

        engine = Mock()
        workflow = _make_workflow(engine, resolving_nodes=["MyNode"])
        workflow.objects.get_as_type.return_value = node

        with patch(
            "griptape_nodes.retained_mode.file_metadata.workflow_metadata._serialize_flow",
            return_value=None,
        ):
            result = collect_workflow_metadata(engine, workflow)

        assert "gtn_param_prompt" in result
        assert "gtn_param_password" not in result
