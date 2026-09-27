"""End-to-end tests for the shipped save_file_sequence and save_output_directory situations.

These run against the real default project template and the real OS and project managers, so
they pin the on-disk layout the shipped macros produce. The mocked unit tests for the parameter
components cannot catch a macro that fails to resolve in a real project.
"""

from pathlib import Path

import pytest

from griptape_nodes.common.project_templates.situation import BuiltInSituation
from griptape_nodes.exe_types.param_components import project_directory_parameter, project_file_sequence_parameter
from griptape_nodes.retained_mode.engine import Engine


@pytest.fixture
def workspace(engine: Engine, tmp_path: Path) -> Path:
    """Point the engine's workspace at a temp directory and return it."""
    engine.config_manager.set_config_value("workspace_directory", str(tmp_path))
    return tmp_path


class TestSaveFileSequenceSituation:
    def test_each_run_writes_frames_into_a_new_version_folder(self, workspace: Path) -> None:
        for _ in range(2):
            destination = project_file_sequence_parameter._build_sequence_destination_from_situation(
                "frames.png", BuiltInSituation.SAVE_FILE_SEQUENCE
            )
            destination.entry(1).write_bytes(b"frame")
            destination.entry(2).write_bytes(b"frame")

        written = sorted(path.relative_to(workspace).as_posix() for path in workspace.rglob("*.png"))

        assert written == [
            "outputs/images/frames_v001/frames.0001.png",
            "outputs/images/frames_v001/frames.0002.png",
            "outputs/images/frames_v002/frames.0001.png",
            "outputs/images/frames_v002/frames.0002.png",
        ]


class TestSaveOutputDirectorySituation:
    def test_each_create_makes_a_new_version_folder(self, workspace: Path) -> None:
        for _ in range(2):
            project_directory_parameter._build_directory_destination_from_situation(
                "renders", BuiltInSituation.SAVE_OUTPUT_DIRECTORY
            ).create()

        created = sorted(path.name for path in (workspace / "outputs").iterdir())

        assert created == ["renders_v001", "renders_v002"]
