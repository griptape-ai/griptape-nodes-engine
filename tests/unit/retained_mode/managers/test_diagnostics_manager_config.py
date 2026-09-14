"""Tests for the config-layer part of the diagnostics report.

`ConfigManager.config_layers` reports all six layers, including the three no file backs.
This section translates that into the two things a reader of a bug report needs: the file
chain, with a missing or broken file still listed, and the pin that no file holds.

Both directions are easy to get wrong. Dropping a layer that reports no path would drop the
workspace pin; keeping one would invent a config file at path `None`. And a layer whose
file exists is not automatically a layer that applied -- a broken file is skipped, and the
workspace layer stands down when its file is the project's.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from griptape_nodes.common.diagnostics.redaction import Redactor
from griptape_nodes.retained_mode.events.config_events import ConfigLayer
from griptape_nodes.retained_mode.managers.diagnostics_manager import DiagnosticsManager

if TYPE_CHECKING:
    from griptape_nodes.common.diagnostics.report import ConfigDiagnostics

_USER_CONFIG = "/home/samantha/.config/griptape_nodes/griptape_nodes_config.json"
_PROJECT_CONFIG = "/home/samantha/projects/demo/griptape_nodes_config.json"


@pytest.fixture
def engine() -> Mock:
    """A stand-in engine, since the layer stack is the only thing this section reads."""
    return Mock()


def _section(engine: Mock, layers: list[ConfigLayer], redactor: Redactor | None = None) -> ConfigDiagnostics:
    """Build the config section from a layer stack.

    Identity normalization defaults to off so an asserted path is the one the layer named.
    What it turns into is `Redactor`'s own contract, tested where the redactor is.
    """
    if redactor is None:
        redactor = Redactor(secret_values=[], normalize_identity=False)

    engine.config_manager.config_layers.return_value = layers
    engine.config_manager.merged_config = {}
    return DiagnosticsManager(Mock(), engine=engine)._build_config_section(redactor)


class TestFileLayers:
    def test_a_layer_no_file_backs_is_not_reported_as_a_config_file(self, engine: Mock) -> None:
        """`default`, `runtime`, and `env` have no path, and a file at `None` is not a file."""
        section = _section(
            engine,
            [
                ConfigLayer(layer="default", path=None, present=True, values={"log_level": "INFO"}),
                ConfigLayer(layer="user", path=_USER_CONFIG, present=True),
                ConfigLayer(layer="env", path=None, present=True, env_vars={"GTN_CONFIG_LOG_LEVEL": "DEBUG"}),
            ],
        )

        assert [entry.layer for entry in section.files] == ["user"]

    def test_a_file_that_does_not_exist_is_still_listed(self, engine: Mock) -> None:
        """A setting read from a file the user did not realize was absent needs the full chain."""
        section = _section(
            engine,
            [ConfigLayer(layer="workspace", path="/home/samantha/GriptapeNodes/nope.json", present=False)],
        )

        assert [(entry.layer, entry.exists) for entry in section.files] == [("workspace", False)]
        assert section.files[0].size_bytes is None

    def test_the_order_is_the_order_the_layers_override_each_other(self, engine: Mock) -> None:
        section = _section(
            engine,
            [
                ConfigLayer(layer="user", path=_USER_CONFIG, present=True),
                ConfigLayer(layer="project", path=_PROJECT_CONFIG, present=True),
            ],
        )

        assert [entry.layer for entry in section.files] == ["user", "project"]

    def test_a_files_size_is_reported_when_it_exists(self, engine: Mock, tmp_path: Path) -> None:
        """A config file of zero bytes explains a setting that will not stick."""
        contents = "{}"
        config_file = tmp_path / "griptape_nodes_config.json"
        config_file.write_text(contents, encoding="utf-8")

        section = _section(engine, [ConfigLayer(layer="user", path=str(config_file), present=True)])

        assert section.files[0].exists is True
        assert section.files[0].size_bytes == len(contents)

    def test_a_broken_file_carries_the_reason_it_did_not_parse(self, engine: Mock) -> None:
        """The merge skips a file it cannot read, so nothing else in the report reveals it."""
        section = _section(
            engine,
            [
                ConfigLayer(
                    layer="user",
                    path=_USER_CONFIG,
                    present=True,
                    parse_error="Expecting ',' delimiter: line 4 column 3 (char 61)",
                )
            ],
        )

        assert section.files[0].parse_error == "Expecting ',' delimiter: line 4 column 3 (char 61)"

    def test_a_layer_standing_down_is_told_apart_from_one_whose_file_is_missing(
        self, engine: Mock, tmp_path: Path
    ) -> None:
        """Workspace dir == project dir: one file, read once, as `project`."""
        config_file = tmp_path / "griptape_nodes_config.json"
        config_file.write_text("{}", encoding="utf-8")

        section = _section(
            engine,
            [
                ConfigLayer(layer="project", path=str(config_file), present=True),
                ConfigLayer(layer="workspace", path=str(config_file), present=False),
            ],
        )

        workspace = section.files[1]
        assert workspace.exists is True
        assert workspace.contributes is False


class TestRuntimeWorkspacePin:
    def test_a_pin_that_is_its_own_layer_is_reported(self, engine: Mock) -> None:
        """No file holds it, so a settings write cannot reach it. Saying so is the point."""
        section = _section(
            engine,
            [
                ConfigLayer(
                    layer="runtime",
                    path=None,
                    present=True,
                    values={"workspace_directory": "/home/samantha/projects/demo/workspace"},
                )
            ],
        )

        assert section.runtime_workspace_pin == "/home/samantha/projects/demo/workspace"

    def test_a_pin_restating_what_a_config_file_supplies_is_not_reported(self, engine: Mock) -> None:
        """An ordinary activation re-applies the user's own value; that file is still the owner."""
        section = _section(
            engine,
            [ConfigLayer(layer="runtime", path=None, present=False, values={})],
        )

        assert section.runtime_workspace_pin is None

    def test_nothing_is_reported_when_the_stack_has_no_runtime_layer(self, engine: Mock) -> None:
        """An older or stubbed ConfigManager should cost this field, not the whole report."""
        section = _section(engine, [ConfigLayer(layer="user", path=_USER_CONFIG, present=True)])

        assert section.runtime_workspace_pin is None

    def test_the_pin_goes_through_the_redactor(
        self, engine: Mock, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """It is a path, usually under the home directory a bug report should not carry."""
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
        pinned = tmp_path / "demo" / "ws"

        section = _section(
            engine,
            [ConfigLayer(layer="runtime", path=None, present=True, values={"workspace_directory": str(pinned)})],
            Redactor(secret_values=[]),
        )

        assert section.runtime_workspace_pin is not None
        assert str(tmp_path) not in section.runtime_workspace_pin
        assert section.runtime_workspace_pin.startswith("~")
