"""Tests for ProjectExporter's downloaded-library sink resolution.

`_resolve_libraries_sink` decides where the engine would clone `libraries_to_download`
so the export can prune that subtree from the mirrored tree (preserving the
reference-only contract). It must mirror the engine's resolution precedence: the
project's own template `libraries_dir` field wins, else `libraries_directory` resolved
against the workspace dir, where the workspace is the template `workspace_dir` field,
else the adjacent config's workspace_directory, else the base dir.
"""

from pathlib import Path

from griptape_nodes.common.project_templates import (
    DEFAULT_PROJECT_TEMPLATE,
    ProjectValidationInfo,
    ProjectValidationStatus,
)
from griptape_nodes.common.project_templates.project_path import PerPlatformProjectPath
from griptape_nodes.retained_mode.managers.project_manager import ProjectInfo
from griptape_nodes.retained_mode.publishing.project_packager import ProjectExporter


def _project_info(base_dir: Path, *, libraries_dir: object = None, workspace_dir: object = None) -> ProjectInfo:
    template = DEFAULT_PROJECT_TEMPLATE.model_copy(
        update={"libraries_dir": libraries_dir, "workspace_dir": workspace_dir}
    )
    return ProjectInfo(
        project_id="test-id",
        project_file_path=base_dir / "griptape-nodes-project.yml",
        project_base_dir=base_dir,
        template=template,
        validation=ProjectValidationInfo(status=ProjectValidationStatus.GOOD),
        parsed_situation_schemas={},
        parsed_directory_schemas={},
    )


def _exporter(base_dir: Path, *, adjacent_config: dict, **info_kwargs: object) -> ProjectExporter:
    info = _project_info(base_dir, **info_kwargs)
    return ProjectExporter(info, adjacent_config, base_dir / "out.zip")


class TestResolveLibrariesSink:
    def test_defaults_to_base_dir_libraries(self, tmp_path: Path) -> None:
        """No template fields and empty config -> <base>/libraries (legacy behavior)."""
        exporter = _exporter(tmp_path, adjacent_config={})
        assert exporter._resolve_libraries_sink() == tmp_path / "libraries"

    def test_adjacent_libraries_directory_relative_to_base(self, tmp_path: Path) -> None:
        exporter = _exporter(tmp_path, adjacent_config={"libraries_directory": "vendored"})
        assert exporter._resolve_libraries_sink() == tmp_path / "vendored"

    def test_adjacent_workspace_directory_relocates_sink(self, tmp_path: Path) -> None:
        """A relative workspace_directory in config moves the workspace-relative sink."""
        exporter = _exporter(tmp_path, adjacent_config={"workspace_directory": "ws"})
        assert exporter._resolve_libraries_sink() == tmp_path / "ws" / "libraries"

    def test_template_workspace_dir_wins_over_config(self, tmp_path: Path) -> None:
        """The template's own workspace_dir is honored over the adjacent config."""
        exporter = _exporter(
            tmp_path,
            adjacent_config={"workspace_directory": "ignored"},
            workspace_dir="ws-from-template",
        )
        assert exporter._resolve_libraries_sink() == tmp_path / "ws-from-template" / "libraries"

    def test_template_libraries_dir_wins_over_everything(self, tmp_path: Path) -> None:
        """The template's own libraries_dir is the highest-priority sink source."""
        exporter = _exporter(
            tmp_path,
            adjacent_config={"workspace_directory": "ws", "libraries_directory": "ignored"},
            workspace_dir="ws",
            libraries_dir="./my-libs",
        )
        assert exporter._resolve_libraries_sink() == tmp_path / "my-libs"

    def test_absolute_template_libraries_dir_used_as_is(self, tmp_path: Path) -> None:
        shared = tmp_path / "shared" / "libraries"
        exporter = _exporter(tmp_path, adjacent_config={}, libraries_dir=str(shared))
        assert exporter._resolve_libraries_sink() == shared

    def test_per_platform_libraries_dir_selects_active(self, tmp_path: Path) -> None:
        """A per-platform libraries_dir reduces to the active platform's value."""
        abs_libs = tmp_path / "platform-libs"
        per_platform = PerPlatformProjectPath(default=str(abs_libs))
        exporter = _exporter(tmp_path, adjacent_config={}, libraries_dir=per_platform)
        assert exporter._resolve_libraries_sink() == abs_libs
