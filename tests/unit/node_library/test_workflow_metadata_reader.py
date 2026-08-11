"""Tests for reading a workflow's metadata header straight off disk."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.node_library.workflow_registry import (
    WorkflowMetadataError,
    find_metadata_blocks,
    read_workflow_metadata,
)

if TYPE_CHECKING:
    from pathlib import Path

_SHAPE = {
    "inputs": {"Start Flow": {"text": {"name": "text", "type": "str", "default_value": ""}}},
    "outputs": {"End Flow": {"result": {"name": "result", "type": "str", "default_value": ""}}},
}


def _header(**overrides: str) -> str:
    fields = {
        "name": '"demo"',
        "schema_version": '"0.20.0"',
        "engine_version_created_with": '"0.0.0"',
        "node_libraries_referenced": '[["Demo Library", "0.1.0"]]',
    }
    fields.update(overrides)
    lines = ["# /// script", "# [tool.griptape-nodes]"]
    lines.extend(f"# {key} = {value}" for key, value in fields.items())
    lines.append("# ///")
    return "\n".join(lines) + "\n"


def _write(tmp_path: Path, content: str) -> Path:
    workflow_path = tmp_path / "demo_workflow.py"
    workflow_path.write_text(content, encoding="utf-8")
    return workflow_path


class TestFindMetadataBlocks:
    def test_finds_matching_block(self) -> None:
        matches = find_metadata_blocks(_header(), "script")
        assert len(matches) == 1
        assert 'name = "demo"' in matches[0].group("content")

    def test_ignores_other_block_names(self) -> None:
        assert find_metadata_blocks(_header(), "other") == []

    def test_no_block_at_all(self) -> None:
        assert find_metadata_blocks("print('hello')\n", "script") == []


class TestReadWorkflowMetadata:
    def test_reads_header_fields(self, tmp_path: Path) -> None:
        workflow_path = _write(tmp_path, _header(description='"A demo"') + "\nprint('body')\n")

        metadata = read_workflow_metadata(workflow_path)

        assert metadata.name == "demo"
        assert metadata.schema_version == "0.20.0"
        assert metadata.description == "A demo"
        assert [library.library_name for library in metadata.node_libraries_referenced] == ["Demo Library"]

    def test_deserializes_workflow_shape(self, tmp_path: Path) -> None:
        shape_json = json.dumps(json.dumps(_SHAPE, separators=(",", ":")))
        workflow_path = _write(tmp_path, _header(workflow_shape=shape_json))

        metadata = read_workflow_metadata(workflow_path)

        assert metadata.workflow_shape is not None
        assert list(metadata.workflow_shape.inputs) == ["Start Flow"]
        assert list(metadata.workflow_shape.outputs) == ["End Flow"]

    def test_shape_absent_is_none(self, tmp_path: Path) -> None:
        assert read_workflow_metadata(_write(tmp_path, _header())).workflow_shape is None

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(WorkflowMetadataError, match="could not be read"):
            read_workflow_metadata(tmp_path / "absent.py")

    def test_no_header(self, tmp_path: Path) -> None:
        with pytest.raises(WorkflowMetadataError, match="0 'script' metadata sections"):
            read_workflow_metadata(_write(tmp_path, "print('hello')\n"))

    def test_two_headers(self, tmp_path: Path) -> None:
        with pytest.raises(WorkflowMetadataError, match="2 'script' metadata sections"):
            read_workflow_metadata(_write(tmp_path, _header() + "\n" + _header()))

    def test_invalid_toml(self, tmp_path: Path) -> None:
        content = "# /// script\n# [tool.griptape-nodes\n# name = broken\n# ///\n"
        with pytest.raises(WorkflowMetadataError, match="not valid TOML"):
            read_workflow_metadata(_write(tmp_path, content))

    def test_missing_tool_table(self, tmp_path: Path) -> None:
        content = '# /// script\n# dependencies = []\n# name = "demo"\n# ///\n'
        with pytest.raises(WorkflowMetadataError, match=r"no '\[tool.griptape-nodes\]' table"):
            read_workflow_metadata(_write(tmp_path, content))

    def test_schema_mismatch(self, tmp_path: Path) -> None:
        content = '# /// script\n# [tool.griptape-nodes]\n# name = "demo"\n# ///\n'
        with pytest.raises(WorkflowMetadataError, match="does not match the expected schema"):
            read_workflow_metadata(_write(tmp_path, content))
