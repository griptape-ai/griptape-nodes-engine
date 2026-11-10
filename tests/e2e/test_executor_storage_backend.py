"""End-to-end coverage for how `LocalWorkflowExecutor` applies a storage backend.

A headless run must use the backend it is given for that run, and must not write it to the
user's config file: that file belongs to the interactive app. Runs in a fresh subprocess
because the engine picks its storage driver once, when it is built, and the executor has to
be created before that happens, as it is in a saved workflow's `__main__`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

# Timeout with thread dump.
pytestmark = pytest.mark.timeout(300, method="thread")

PROBE_SCRIPT = """
from griptape_nodes.bootstrap.workflow_executors.local_workflow_executor import LocalWorkflowExecutor
from griptape_nodes.drivers.storage import StorageBackend
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

LocalWorkflowExecutor(storage_backend=StorageBackend.LOCAL, skip_library_loading=True)
print(f"STORAGE_BACKEND={GriptapeNodes.StaticFilesManager().storage_backend}", flush=True)
"""


def test_explicit_storage_backend_applies_to_the_run_without_writing_config(
    tmp_path: Path,
    engine_subprocess_env: Callable[..., dict[str, str]],
) -> None:
    """A `local` backend passed to the executor wins over a configured `gtc`, and the file keeps `gtc`."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_root = tmp_path / "xdg_config"
    config_dir = config_root / "griptape_nodes"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "griptape_nodes_config.json"
    config_path.write_text(
        json.dumps({"workspace_directory": str(workspace), "log_level": "WARNING", "storage_backend": "gtc"})
    )
    probe_path = tmp_path / "probe.py"
    probe_path.write_text(PROBE_SCRIPT)

    result = subprocess.run(  # noqa: S603 - subprocess input is constructed inside the test
        [sys.executable, str(probe_path)],
        env=engine_subprocess_env(XDG_CONFIG_HOME=str(config_root)),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    diagnostic = f"exit code: {result.returncode}\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    assert result.returncode == 0, diagnostic
    # The backend the storage driver was chosen from. Not the driver itself: without cloud
    # credentials a `gtc` backend falls back to the local driver, which would hide the bug.
    assert "STORAGE_BACKEND=local" in result.stdout, diagnostic
    assert json.loads(config_path.read_text())["storage_backend"] == "gtc"
