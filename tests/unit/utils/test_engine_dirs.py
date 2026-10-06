from pathlib import Path
from typing import NamedTuple

import pytest

from griptape_nodes.utils import engine_dirs
from griptape_nodes.utils.engine_dirs import engine_config_dir, engine_data_dir, engine_state_dir


class XdgHomes(NamedTuple):
    config: Path
    data: Path
    state: Path


_ENV_VARS = ("GTN_CONFIG_DIR", "GTN_DATA_DIR", "GTN_STATE_DIR")


@pytest.fixture
def xdg_homes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> XdgHomes:
    """Start each test with no overrides set and the XDG base directories under `tmp_path`."""
    for env_var in _ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)

    homes = XdgHomes(config=tmp_path / "xdg_config", data=tmp_path / "xdg_data", state=tmp_path / "xdg_state")
    monkeypatch.setattr(engine_dirs, "xdg_config_home", lambda: homes.config)
    monkeypatch.setattr(engine_dirs, "xdg_data_home", lambda: homes.data)
    monkeypatch.setattr(engine_dirs, "xdg_state_home", lambda: homes.state)
    return homes


class TestEngineDirs:
    def test_defaults_to_xdg_griptape_nodes_dirs(self, xdg_homes: XdgHomes) -> None:
        assert engine_config_dir() == xdg_homes.config / "griptape_nodes"
        assert engine_data_dir() == xdg_homes.data / "griptape_nodes"
        assert engine_state_dir() == xdg_homes.state / "griptape_nodes"

    def test_absolute_override_is_used_as_is(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("GTN_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("GTN_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("GTN_STATE_DIR", str(tmp_path / "state"))

        assert engine_config_dir() == tmp_path / "config"
        assert engine_data_dir() == tmp_path / "data"
        assert engine_state_dir() == tmp_path / "state"

    @pytest.mark.parametrize("relative_value", ["relative/path", "./here", "~", ""])
    def test_relative_or_empty_override_is_ignored(
        self, xdg_homes: XdgHomes, monkeypatch: pytest.MonkeyPatch, relative_value: str
    ) -> None:
        monkeypatch.setenv("GTN_CONFIG_DIR", relative_value)
        monkeypatch.setenv("GTN_DATA_DIR", relative_value)
        monkeypatch.setenv("GTN_STATE_DIR", relative_value)

        assert engine_config_dir() == xdg_homes.config / "griptape_nodes"
        assert engine_data_dir() == xdg_homes.data / "griptape_nodes"
        assert engine_state_dir() == xdg_homes.state / "griptape_nodes"

    def test_each_variable_only_affects_its_own_directory(
        self, xdg_homes: XdgHomes, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("GTN_DATA_DIR", str(tmp_path))

        assert engine_data_dir() == tmp_path
        assert engine_config_dir() == xdg_homes.config / "griptape_nodes"
        assert engine_state_dir() == xdg_homes.state / "griptape_nodes"

    @pytest.mark.usefixtures("xdg_homes")
    def test_override_is_read_at_call_time(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        before = engine_data_dir()
        monkeypatch.setenv("GTN_DATA_DIR", str(tmp_path))

        assert engine_data_dir() == tmp_path
        assert before != tmp_path
