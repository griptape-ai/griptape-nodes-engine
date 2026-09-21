"""End-to-end round trips through the real engine for `File` paths."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from griptape_nodes.files.file import File

if TYPE_CHECKING:
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine


class TestRelativePathRoundTrip:
    @pytest.fixture
    def cwd_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Run the test from a directory that is *not* the workspace.

        Depends on `workspace_path` purely for ordering: the engine and its
        ConfigManager must exist before the process working directory moves.
        `monkeypatch.chdir` restores the original directory automatically.
        """
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        return cwd

    @pytest.mark.parametrize(
        "relative_path",
        ["config.json", "data/nested.json", pytest.param("foo%20bar.txt"), pytest.param("report$.txt")],
    )
    def test_write_then_read_resolves_to_the_workspace(
        self, relative_path: str, cwd_path: Path, engine: Engine
    ) -> None:
        content = f"round trip content for {relative_path}"

        written_path = File(relative_path).write_text(content)

        assert File(relative_path).read_text() == content
        # Compare against the ConfigManager's own getter, which is `.resolve()`d, so a
        # symlinked temp directory (macOS `/tmp` -> `/private/tmp`) does not fail this.
        assert written_path == engine.config_manager.workspace_path / relative_path
        assert not (cwd_path / relative_path).exists()
