"""Unit tests for workflow_metadata.py provenance collection functions."""

import logging
from typing import Any
from unittest.mock import Mock, patch

import pytest

from griptape_nodes.exe_types.core_types import ParameterMode
from griptape_nodes.retained_mode.file_metadata.workflow_metadata import (
    _collect_parameter_values,
    _collect_raw_provenance,
    _collect_workflow_info,
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


def _make_engine(
    *,
    has_workflow: bool = True,
    workflow_name: str = "test_workflow",
    has_flow: bool = True,
    flow_name: str = "ControlFlow_1",
    resolving_nodes: list[str] | None = None,
) -> Mock:
    """Build a minimal mock engine for provenance tests."""
    engine = Mock()
    ctx = engine.context_manager
    ctx.has_current_workflow.return_value = has_workflow
    ctx.get_current_workflow_name.return_value = workflow_name
    ctx.has_current_flow.return_value = has_flow

    flow = Mock()
    flow.name = flow_name
    ctx.get_current_flow.return_value = flow
    engine.flow_manager.flow_state.return_value = (None, resolving_nodes or [], None)

    return engine


class TestCollectWorkflowInfo:
    def test_returns_name_when_registry_lookup_fails(self) -> None:
        with patch(
            "griptape_nodes.retained_mode.file_metadata.workflow_metadata.WorkflowRegistry.get_workflow_by_name",
            side_effect=KeyError("not found"),
        ):
            result = _collect_workflow_info("my_workflow")

        assert result == {"name": "my_workflow"}

    def test_includes_dates_and_version_when_present(self) -> None:
        from datetime import UTC, datetime

        workflow = Mock()
        workflow.metadata.creation_date = datetime(2026, 1, 1, tzinfo=UTC)
        workflow.metadata.last_modified_date = datetime(2026, 6, 1, tzinfo=UTC)
        workflow.metadata.engine_version_created_with = "1.2.3"
        workflow.metadata.description = None

        with patch(
            "griptape_nodes.retained_mode.file_metadata.workflow_metadata.WorkflowRegistry.get_workflow_by_name",
            return_value=workflow,
        ):
            result = _collect_workflow_info("my_workflow")

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

        with patch(
            "griptape_nodes.retained_mode.file_metadata.workflow_metadata.WorkflowRegistry.get_workflow_by_name",
            return_value=workflow,
        ):
            result = _collect_workflow_info("my_workflow")

        assert result == {"name": "my_workflow"}


class TestCollectParameterValues:
    def test_returns_none_when_node_not_found(self, caplog: pytest.LogCaptureFixture) -> None:
        engine = Mock()
        engine.object_manager.attempt_get_object_by_name_as_type.return_value = None

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            result = _collect_parameter_values("missing_node", engine)

        assert result is None
        assert "missing_node" in caplog.text

    def test_returns_none_when_node_lookup_raises(self, caplog: pytest.LogCaptureFixture) -> None:
        engine = Mock()
        engine.object_manager.attempt_get_object_by_name_as_type.side_effect = RuntimeError("boom")

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            result = _collect_parameter_values("bad_node", engine)

        assert result is None
        assert "bad_node" in caplog.text

    def test_collects_non_excluded_parameters(self) -> None:
        p1, _ = _make_param("prompt", value=None)
        p2, _ = _make_param("model", value=None)

        values = {"prompt": "hello", "model": "gpt-4"}
        node = Mock()
        node.parameters = [p1, p2]
        node.get_parameter_value.side_effect = values.get

        engine = Mock()
        engine.object_manager.attempt_get_object_by_name_as_type.return_value = node

        result = _collect_parameter_values("my_node", engine)

        assert result is not None
        assert result.values == {"prompt": "hello", "model": "gpt-4"}
        assert result.omitted == []

    def test_excludes_metadata_excluded_parameters_from_values(self) -> None:
        pub, _ = _make_param("prompt", value=None)
        priv, _ = _make_param("password", exclude_from_metadata=True, value=None)

        values = {"prompt": "hello", "password": "secret"}
        node = Mock()
        node.parameters = [pub, priv]
        node.get_parameter_value.side_effect = values.get

        engine = Mock()
        engine.object_manager.attempt_get_object_by_name_as_type.return_value = node

        result = _collect_parameter_values("my_node", engine)

        assert result is not None
        assert "password" not in result.values
        assert "prompt" in result.values
        assert result.omitted == ["password"]

    def test_skips_none_values(self) -> None:
        p, _ = _make_param("optional_param", value=None)

        node = Mock()
        node.parameters = [p]
        node.get_parameter_value.return_value = None

        engine = Mock()
        engine.object_manager.attempt_get_object_by_name_as_type.return_value = node

        result = _collect_parameter_values("my_node", engine)

        assert result is not None
        assert result.values == {}
        assert result.omitted == []

    def test_skips_output_only_parameters(self) -> None:
        p, _ = _make_param("output_image", value=None, modes={ParameterMode.OUTPUT})

        node = Mock()
        node.parameters = [p]
        node.get_parameter_value.return_value = "some_value"

        engine = Mock()
        engine.object_manager.attempt_get_object_by_name_as_type.return_value = node

        result = _collect_parameter_values("my_node", engine)

        assert result is not None
        assert result.values == {}

    def test_multiple_excluded_params_all_listed_in_omitted(self) -> None:
        p1, _ = _make_param("api_password", exclude_from_metadata=True, value=None)
        p2, _ = _make_param("db_password", exclude_from_metadata=True, value=None)
        p3, _ = _make_param("prompt", value=None)

        values = {"api_password": "s3cr3t", "db_password": "hunter2", "prompt": "hello"}
        node = Mock()
        node.parameters = [p1, p2, p3]
        node.get_parameter_value.side_effect = values.get

        engine = Mock()
        engine.object_manager.attempt_get_object_by_name_as_type.return_value = node

        result = _collect_parameter_values("my_node", engine)

        assert result is not None
        assert set(result.omitted) == {"api_password", "db_password"}
        assert result.values == {"prompt": "hello"}


class TestCollectRawProvenance:
    def test_returns_empty_when_no_workflow_context(self) -> None:
        engine = _make_engine(has_workflow=False)

        result = _collect_raw_provenance(engine)

        assert result == {}

    def test_returns_workflow_only_when_no_flow(self) -> None:
        engine = _make_engine(has_flow=False)

        with patch(
            "griptape_nodes.retained_mode.file_metadata.workflow_metadata.WorkflowRegistry.get_workflow_by_name",
            side_effect=KeyError,
        ):
            result = _collect_raw_provenance(engine)

        assert "workflow" in result
        assert "flow" not in result

    def test_includes_flow_and_resolving_nodes(self) -> None:
        engine = _make_engine(resolving_nodes=["MyNode"])
        engine.object_manager.attempt_get_object_by_name_as_type.return_value = None

        with patch(
            "griptape_nodes.retained_mode.file_metadata.workflow_metadata.WorkflowRegistry.get_workflow_by_name",
            side_effect=KeyError,
        ):
            result = _collect_raw_provenance(engine)

        assert result["flow"]["name"] == "ControlFlow_1"
        assert result["flow"]["resolving_nodes"] == ["MyNode"]

    def test_parameters_omitted_present_for_excluded_params(self) -> None:
        priv, _ = _make_param("password", exclude_from_metadata=True, value=None)
        pub, _ = _make_param("prompt", value=None)

        param_values = {"password": "secret", "prompt": "hi"}
        node = Mock()
        node.parameters = [priv, pub]
        node.get_parameter_value.side_effect = param_values.get

        engine = _make_engine(resolving_nodes=["MyNode"])
        engine.object_manager.attempt_get_object_by_name_as_type.return_value = node

        with patch(
            "griptape_nodes.retained_mode.file_metadata.workflow_metadata.WorkflowRegistry.get_workflow_by_name",
            side_effect=KeyError,
        ):
            result = _collect_raw_provenance(engine)

        assert "parameters_omitted" in result
        assert "password" in result["parameters_omitted"]
        assert "password" not in result.get("parameters", {})

    def test_no_parameters_key_when_no_resolving_nodes(self) -> None:
        engine = _make_engine(resolving_nodes=[])

        with patch(
            "griptape_nodes.retained_mode.file_metadata.workflow_metadata.WorkflowRegistry.get_workflow_by_name",
            side_effect=KeyError,
        ):
            result = _collect_raw_provenance(engine)

        assert "parameters" not in result


class TestCollectSidecarProvenance:
    def test_empty_when_no_context(self) -> None:
        engine = _make_engine(has_workflow=False)

        result = collect_sidecar_provenance(engine)

        assert result == {}

    def test_flow_block_uses_first_resolving_node_as_node_name(self) -> None:
        engine = _make_engine(resolving_nodes=["NodeA", "NodeB"])
        engine.object_manager.attempt_get_object_by_name_as_type.return_value = None

        with patch(
            "griptape_nodes.retained_mode.file_metadata.workflow_metadata.WorkflowRegistry.get_workflow_by_name",
            side_effect=KeyError,
        ):
            result = collect_sidecar_provenance(engine)

        assert result["flow"]["node_name"] == "NodeA"
        assert "resolving_nodes" not in result["flow"]

    def test_excluded_params_in_parameters_omitted_not_parameters(self) -> None:
        priv, _ = _make_param("password", exclude_from_metadata=True, value=None)
        pub, _ = _make_param("prompt", value=None)

        param_values = {"password": "s3cr3t", "prompt": "hi"}
        node = Mock()
        node.parameters = [priv, pub]
        node.get_parameter_value.side_effect = param_values.get

        engine = _make_engine(resolving_nodes=["MyNode"])
        engine.object_manager.attempt_get_object_by_name_as_type.return_value = node

        with patch(
            "griptape_nodes.retained_mode.file_metadata.workflow_metadata.WorkflowRegistry.get_workflow_by_name",
            side_effect=KeyError,
        ):
            result = collect_sidecar_provenance(engine)

        assert result["parameters"] == {"prompt": "hi"}
        assert result["parameters_omitted"] == ["password"]

    def test_parameters_omitted_absent_when_no_excluded_params(self) -> None:
        pub, _ = _make_param("prompt", value=None)

        node = Mock()
        node.parameters = [pub]
        node.get_parameter_value.return_value = "hello"

        engine = _make_engine(resolving_nodes=["MyNode"])
        engine.object_manager.attempt_get_object_by_name_as_type.return_value = node

        with patch(
            "griptape_nodes.retained_mode.file_metadata.workflow_metadata.WorkflowRegistry.get_workflow_by_name",
            side_effect=KeyError,
        ):
            result = collect_sidecar_provenance(engine)

        assert "parameters_omitted" not in result
        assert "prompt" in result["parameters"]

    def test_no_flow_block_when_no_flow_context(self) -> None:
        engine = _make_engine(has_flow=False)

        with patch(
            "griptape_nodes.retained_mode.file_metadata.workflow_metadata.WorkflowRegistry.get_workflow_by_name",
            side_effect=KeyError,
        ):
            result = collect_sidecar_provenance(engine)

        assert "workflow" in result
        assert "flow" not in result
        assert "parameters" not in result


class TestCollectWorkflowMetadata:
    def test_returns_saved_at_with_no_context(self) -> None:
        engine = _make_engine(has_workflow=False)

        result = collect_workflow_metadata(engine)

        assert "gtn_saved_at" in result

    def test_excluded_params_excluded_from_png_metadata(self) -> None:
        priv, _ = _make_param("password", exclude_from_metadata=True, value=None)
        pub, _ = _make_param("prompt", value=None)

        param_values = {"password": "s3cr3t", "prompt": "hello"}
        node = Mock()
        node.parameters = [priv, pub]
        node.get_parameter_value.side_effect = param_values.get

        engine = _make_engine(resolving_nodes=["MyNode"])
        engine.object_manager.attempt_get_object_by_name_as_type.return_value = node

        with (
            patch(
                "griptape_nodes.retained_mode.file_metadata.workflow_metadata.WorkflowRegistry.get_workflow_by_name",
                side_effect=KeyError,
            ),
            patch(
                "griptape_nodes.retained_mode.file_metadata.workflow_metadata._serialize_flow",
                return_value=None,
            ),
        ):
            result = collect_workflow_metadata(engine)

        assert "gtn_param_prompt" in result
        assert "gtn_param_password" not in result
