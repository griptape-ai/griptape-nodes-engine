from pathlib import Path

import pytest

from griptape_nodes.agents.cli import windows_shims
from griptape_nodes.agents.cli.windows_shims import launch_command, shim_script

# npm shim for `npm install -g @anthropic-ai/claude-code`.
_NPM_SHIM = (
    "@ECHO off\r\n"
    'IF EXIST "%dp0%\\node.exe" (\r\n  SET "_prog=%dp0%\\node.exe"\r\n) ELSE (\r\n  SET "_prog=node"\r\n)\r\n'
    'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  '
    '"%dp0%\\node_modules\\@anthropic-ai\\claude-code\\cli.js" %*\r\n'
)


@pytest.fixture
def npm_shim(tmp_path: Path) -> Path:
    """Write an npm shim and the script it names."""
    script = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
    script.parent.mkdir(parents=True)
    script.write_text("")
    shim = tmp_path / "claude.cmd"
    shim.write_text(_NPM_SHIM)
    return shim


def test_shim_script_is_found(npm_shim: Path) -> None:
    """Find the script an npm shim runs."""
    assert shim_script(npm_shim) == npm_shim.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"


def test_other_batch_files_have_no_script(tmp_path: Path) -> None:
    """Return nothing for a batch file that isn't an npm shim."""
    batch = tmp_path / "tool.cmd"
    batch.write_text("@echo off\r\ntool.exe %*\r\n")

    assert shim_script(batch) is None


def test_windows_runs_the_shim_script_with_node(
    npm_shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skip cmd.exe by running node on the script."""
    monkeypatch.setattr(windows_shims.sys, "platform", "win32")
    node = tmp_path / "node.exe"
    node.write_text("")

    assert launch_command(str(npm_shim)) == [str(node), str(shim_script(npm_shim))]


def test_executables_are_run_as_they_are(monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave anything but a shim alone."""
    monkeypatch.setattr(windows_shims.sys, "platform", "win32")

    assert launch_command("C:\\bin\\claude.exe") == ["C:\\bin\\claude.exe"]
