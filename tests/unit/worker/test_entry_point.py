"""`python -m griptape_nodes.worker`, the entry point a library's worker process starts from.

A worker's environment needs only griptape-nodes-engine and the library's own dependencies, which is what
lets an environment tool build it through `worker.command_prefix`. So nothing a worker imports may reach for
the Griptape Nodes app.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

from griptape_nodes.utils.engine_dirs import CONFIG_DIR_ENV_VAR, DATA_DIR_ENV_VAR, STATE_DIR_ENV_VAR
from griptape_nodes.worker.channel import CHANNEL_ADDRESS_ENV, CHANNEL_AUTHKEY_ENV

if TYPE_CHECKING:
    from pathlib import Path

# A cold interpreter importing the engine on a loaded Windows CI box.
_TIMEOUT_S = 180

_IMPORTS_NO_APP = """
import sys

import griptape_nodes.worker.__main__
from griptape_nodes.retained_mode.engine import Engine

Engine()
leaked = sorted(name for name in sys.modules if name.split(".")[0] == "griptape_nodes_app")
print(leaked)
sys.exit(1 if leaked else 0)
"""


def _isolated_environ(tmp_path: Path) -> dict[str, str]:
    environ = {
        name: value for name, value in os.environ.items() if name not in (CHANNEL_ADDRESS_ENV, CHANNEL_AUTHKEY_ENV)
    }
    for variable, directory in (
        (CONFIG_DIR_ENV_VAR, "config"),
        (DATA_DIR_ENV_VAR, "data"),
        (STATE_DIR_ENV_VAR, "state"),
    ):
        (tmp_path / directory).mkdir()
        environ[variable] = str(tmp_path / directory)
    return environ


def test_the_worker_and_the_engine_it_builds_never_import_the_app(tmp_path: Path) -> None:
    """Imports the entry point and builds an engine in a fresh interpreter, then checks what loaded."""
    result = subprocess.run(  # noqa: S603 (the test's own interpreter and script)
        [sys.executable, "-c", _IMPORTS_NO_APP],
        env=_isolated_environ(tmp_path),
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_started_by_hand_it_says_who_starts_it_and_exits(tmp_path: Path) -> None:
    """Without the channel its engine hands it, a worker has nothing to work for."""
    result = subprocess.run(
        [sys.executable, "-m", "griptape_nodes.worker", "--library-name", "Some Library"],
        env=_isolated_environ(tmp_path),
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
        check=False,
    )

    assert result.returncode == 1
    assert "could not reach the engine that started it" in result.stderr
