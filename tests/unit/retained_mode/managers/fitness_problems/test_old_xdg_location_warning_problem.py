from pathlib import Path

import pytest

from griptape_nodes.retained_mode.managers.fitness_problems.libraries import OldXdgLocationWarningProblem


class TestOldXdgLocationWarningProblem:
    def test_collate_names_engine_data_libraries_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("GTN_ENGINE_DATA_DIR", str(tmp_path))
        problem = OldXdgLocationWarningProblem(old_path=str(tmp_path / "libraries" / "my_lib"))

        result = OldXdgLocationWarningProblem.collate_problems_for_display([problem])

        assert str(tmp_path / "libraries") in result
