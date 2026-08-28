"""Tests for `gtn config prune-libraries`.

Editing library settings used to persist the whole MERGED list into the user config, so a
user config can hold library paths belonging to a project opened long ago. Those entries are
inert while a project supplies its own list (lists replace rather than merge) and become the
engine's library set the moment no project layer is loaded to shadow them.

The command reports by default and removes nothing without being asked, because "outside the
libraries directory" is a signal and not proof: registering a library by absolute path from
anywhere is legitimate.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from griptape_nodes.cli.commands import config as config_cli
from griptape_nodes.retained_mode.managers.settings import LIBRARIES_TO_REGISTER_KEY

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def libraries_root(tmp_path: Path) -> Path:
    """The resolved libraries directory entries are measured against."""
    root = tmp_path / "libraries"
    root.mkdir()
    return root


def _make_library(directory: Path, name: str) -> str:
    """Create a library manifest on disk and return its path as config would hold it."""
    lib_dir = directory / name
    lib_dir.mkdir(parents=True, exist_ok=True)
    manifest = lib_dir / "griptape_nodes_library.json"
    manifest.write_text("{}", encoding="utf-8")
    return str(manifest)


@pytest.fixture
def fake_config_manager(monkeypatch: pytest.MonkeyPatch, libraries_root: Path) -> MagicMock:
    """Stand in for the module-level ConfigManager the CLI builds at import time."""
    manager = MagicMock()
    manager.resolved_libraries_root.return_value = libraries_root
    manager.set_config_value.return_value = True
    monkeypatch.setattr(config_cli, "config_manager", manager)
    return manager


def _configure(manager: MagicMock, *, user_entries: list, shadowing: list | None = None) -> None:
    """Wire get_config_value to answer per config_source, as the real manager does."""

    def get_config_value(
        key: str, *, config_source: str = "merged_config", default: object = None, **_: object
    ) -> object:
        if key != LIBRARIES_TO_REGISTER_KEY:
            return default
        if config_source == "user_config":
            return user_entries
        if config_source == "merged_without_user_config":
            return shadowing if shadowing is not None else []
        return default

    manager.get_config_value.side_effect = get_config_value


@pytest.fixture
def user_config_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect the CLI's view of the user config to a temp file it may back up."""
    path = tmp_path / "griptape_nodes_config.json"
    path.write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setattr(config_cli.config_manager_module, "USER_CONFIG_PATH", path)
    return path


class TestClassifyUserLibraryEntry:
    def test_an_entry_inside_the_libraries_root_is_unremarkable(self, libraries_root: Path) -> None:
        path = _make_library(libraries_root, "mine")
        assert config_cli._classify_user_library_entry(path, libraries_root) == ""

    def test_an_entry_outside_the_libraries_root_is_flagged(self, libraries_root: Path, tmp_path: Path) -> None:
        path = _make_library(tmp_path / "some-other-tree", "theirs")
        assert config_cli._classify_user_library_entry(path, libraries_root) == "outside the libraries directory"

    def test_a_nonexistent_path_is_flagged(self, libraries_root: Path) -> None:
        assert (
            config_cli._classify_user_library_entry(str(libraries_root / "gone.json"), libraries_root)
            == "missing on disk"
        )

    def test_an_entry_with_no_path_is_flagged(self, libraries_root: Path) -> None:
        assert config_cli._classify_user_library_entry("", libraries_root) == "no path"


class TestPruneReportsWithoutChanging:
    def test_an_empty_user_list_writes_nothing(self, fake_config_manager: MagicMock) -> None:
        _configure(fake_config_manager, user_entries=[])

        config_cli._prune_user_config_libraries(remove_outside_root=False, remove_paths=[], assume_yes=True)

        fake_config_manager.set_config_value.assert_not_called()

    def test_the_default_run_changes_nothing(
        self, fake_config_manager: MagicMock, libraries_root: Path, tmp_path: Path
    ) -> None:
        """Reporting must be safe to run: no writes, even with flaggable entries present."""
        stale = _make_library(tmp_path / "previous-project", "stale")
        _configure(fake_config_manager, user_entries=[_make_library(libraries_root, "mine"), stale])

        config_cli._prune_user_config_libraries(remove_outside_root=False, remove_paths=[], assume_yes=True)

        fake_config_manager.set_config_value.assert_not_called()


class TestPruneRemovals:
    @pytest.mark.usefixtures("user_config_file")
    def test_remove_outside_root_keeps_entries_under_the_root(
        self, fake_config_manager: MagicMock, libraries_root: Path, tmp_path: Path
    ) -> None:
        mine = _make_library(libraries_root, "mine")
        stale = _make_library(tmp_path / "previous-project", "stale")
        _configure(fake_config_manager, user_entries=[mine, stale])

        config_cli._prune_user_config_libraries(remove_outside_root=True, remove_paths=[], assume_yes=True)

        fake_config_manager.set_config_value.assert_called_once_with(LIBRARIES_TO_REGISTER_KEY, [mine])

    @pytest.mark.usefixtures("user_config_file")
    def test_remove_path_removes_exactly_that_entry(self, fake_config_manager: MagicMock, libraries_root: Path) -> None:
        """The surgical option, for an entry inside the root that the user still wants gone."""
        keep = _make_library(libraries_root, "keep")
        drop = _make_library(libraries_root, "drop")
        _configure(fake_config_manager, user_entries=[keep, drop])

        config_cli._prune_user_config_libraries(remove_outside_root=False, remove_paths=[drop], assume_yes=True)

        fake_config_manager.set_config_value.assert_called_once_with(LIBRARIES_TO_REGISTER_KEY, [keep])

    @pytest.mark.usefixtures("user_config_file")
    def test_dict_shaped_entries_survive_intact(
        self, fake_config_manager: MagicMock, libraries_root: Path, tmp_path: Path
    ) -> None:
        """Entries carrying `enabled` / `worker_mode_override` must be kept whole, not flattened."""
        mine = {"path": _make_library(libraries_root, "mine"), "enabled": False, "worker_mode_override": "WORKER"}
        stale = {"path": _make_library(tmp_path / "previous-project", "stale"), "enabled": True}
        _configure(fake_config_manager, user_entries=[mine, stale])

        config_cli._prune_user_config_libraries(remove_outside_root=True, remove_paths=[], assume_yes=True)

        fake_config_manager.set_config_value.assert_called_once_with(LIBRARIES_TO_REGISTER_KEY, [mine])

    def test_a_backup_is_written_before_the_config_changes(
        self, fake_config_manager: MagicMock, user_config_file: Path, tmp_path: Path
    ) -> None:
        user_config_file.write_text(json.dumps({"marker": "original"}), encoding="utf-8")
        _configure(fake_config_manager, user_entries=[_make_library(tmp_path / "previous-project", "stale")])

        config_cli._prune_user_config_libraries(remove_outside_root=True, remove_paths=[], assume_yes=True)

        backup = user_config_file.with_suffix(".prune.bak")
        assert json.loads(backup.read_text(encoding="utf-8")) == {"marker": "original"}

    @pytest.mark.usefixtures("user_config_file")
    def test_a_failed_write_exits_nonzero(self, fake_config_manager: MagicMock, tmp_path: Path) -> None:
        """A write that did not land must not be reported as a successful prune."""
        _configure(fake_config_manager, user_entries=[_make_library(tmp_path / "previous-project", "stale")])
        fake_config_manager.set_config_value.return_value = False

        with pytest.raises(SystemExit) as exit_info:
            config_cli._prune_user_config_libraries(remove_outside_root=True, remove_paths=[], assume_yes=True)

        assert exit_info.value.code == 1

    def test_an_unmatched_remove_path_removes_nothing(
        self, fake_config_manager: MagicMock, libraries_root: Path
    ) -> None:
        keep = _make_library(libraries_root, "keep")
        _configure(fake_config_manager, user_entries=[keep])

        config_cli._prune_user_config_libraries(
            remove_outside_root=False, remove_paths=["/not/in/the/list.json"], assume_yes=True
        )

        fake_config_manager.set_config_value.assert_not_called()


def _rendered(capsys: pytest.CaptureFixture[str]) -> str:
    """Captured console text with whitespace collapsed.

    The console hard-wraps to the terminal width, so a phrase can be split across lines.
    Asserting on the raw capture makes these tests fail on layout rather than content.
    """
    return " ".join(capsys.readouterr().out.split())


class TestShadowingIsSurfaced:
    def test_a_project_supplied_list_is_reported_as_shadowing(
        self, fake_config_manager: MagicMock, libraries_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The user needs to know their entries are inert right now but will not stay that way."""
        _configure(
            fake_config_manager,
            user_entries=[_make_library(libraries_root, "mine")],
            shadowing=["/show/project/libraries/whatever/griptape_nodes_library.json"],
        )

        config_cli._prune_user_config_libraries(remove_outside_root=False, remove_paths=[], assume_yes=True)

        assert "none of the entries below are currently in effect" in _rendered(capsys)

    def test_no_shadowing_note_when_no_other_layer_supplies_the_key(
        self, fake_config_manager: MagicMock, libraries_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _configure(fake_config_manager, user_entries=[_make_library(libraries_root, "mine")], shadowing=[])

        config_cli._prune_user_config_libraries(remove_outside_root=False, remove_paths=[], assume_yes=True)

        assert "none of the entries below are currently in effect" not in _rendered(capsys)
