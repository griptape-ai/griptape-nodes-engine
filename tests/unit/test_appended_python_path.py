import os
import subprocess
import sys
from pathlib import Path

import pytest

from griptape_nodes.appended_python_path import APPEND_PYTHONPATH_ENV_VAR, append_python_path_from_environment


class TestAppendPythonPathFromEnvironment:
    def test_unset_leaves_search_path_alone(self) -> None:
        search_path = ["/engine"]

        append_python_path_from_environment({}, search_path)

        assert search_path == ["/engine"]

    def test_entries_go_after_existing_ones_in_order(self, tmp_path: Path) -> None:
        first = str(tmp_path / "first")
        second = str(tmp_path / "second")
        search_path = ["/engine"]

        append_python_path_from_environment({APPEND_PYTHONPATH_ENV_VAR: first + os.pathsep + second}, search_path)

        assert search_path == ["/engine", first, second]

    def test_skips_empty_and_already_present_entries(self, tmp_path: Path) -> None:
        present = str(tmp_path / "present")
        search_path = [present]

        append_python_path_from_environment(
            {APPEND_PYTHONPATH_ENV_VAR: os.pathsep + present + os.pathsep + os.pathsep}, search_path
        )

        assert search_path == [present]

    def test_relative_entries_become_absolute(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        search_path: list[str] = []

        append_python_path_from_environment({APPEND_PYTHONPATH_ENV_VAR: "extra"}, search_path)

        assert search_path == [str(tmp_path / "extra")]


def test_engine_packages_win_over_appended_ones(tmp_path: Path) -> None:
    """An appended copy of an engine dependency must not shadow it, while other appended modules import."""
    shadow = tmp_path / "pydantic"
    shadow.mkdir()
    (shadow / "__init__.py").write_text("raise ImportError('appended pydantic shadowed the engine')\n")
    (tmp_path / "extra_tools.py").write_text("NAME = 'extra'\n")
    environ = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    environ[APPEND_PYTHONPATH_ENV_VAR] = str(tmp_path)

    result = subprocess.run(
        [sys.executable, "-c", "import griptape_nodes, pydantic, extra_tools; print(extra_tools.NAME)"],
        env=environ,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "extra"
