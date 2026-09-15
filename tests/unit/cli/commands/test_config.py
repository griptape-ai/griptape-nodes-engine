"""Unit tests for the config CLI command."""

import io

import pytest
from rich.console import Console

from griptape_nodes.cli.commands import config as config_command


class TestListUserConfigs:
    def test_lists_user_configs_on_cp1252_stdout(self, monkeypatch):
        """Regression test for issue #5470.

        `gtn config list` used to raise UnicodeEncodeError on Windows consoles
        whose stdout encoding is the legacy cp1252 code page, because the
        header contained the U+27F6 (long rightwards arrow) character.
        """
        cp1252_console = Console(
            file=io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict"),
            force_terminal=False,
        )
        monkeypatch.setattr(config_command, "console", cp1252_console)

        config_command._list_user_configs()  # must not raise UnicodeEncodeError

        output = cp1252_console.file.buffer.getvalue().decode("cp1252")
        assert "User Configuration Files" in output
        assert "highest precedence" in output
