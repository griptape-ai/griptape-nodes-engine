import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from griptape_nodes.agents.cli import process

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Process groups are POSIX-only.")

_CLI_LEAVING_A_CHILD = """
import json, subprocess, sys
stdout = subprocess.DEVNULL if sys.argv[1] == "detached" else None
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdout=stdout, stderr=stdout)
print(json.dumps({"child_pid": child.pid}), flush=True)
"""


@pytest.fixture(autouse=True)
def short_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the cleanup waits short."""
    monkeypatch.setattr(process, "_TERMINATE_GRACE_SECONDS", 0.5)


async def _run(tmp_path: Path, mode: str) -> tuple[process.ProcessExit, list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []

    async def on_record(record: dict[str, Any]) -> None:
        records.append(record)

    exit_info = await asyncio.wait_for(
        process.run_jsonl_process(
            [sys.executable, "-c", _CLI_LEAVING_A_CHILD, mode],
            cwd=tmp_path,
            env=os.environ,
            stdin=None,
            on_record=on_record,
            cancel_event=None,
        ),
        timeout=10,
    )
    return exit_info, records


def _is_running(pid: int) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        time.sleep(0.05)
    return True


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["holding_pipes", "detached"])
async def test_children_left_running_are_stopped(tmp_path: Path, mode: str) -> None:
    """A child holding the CLI's pipes must not hang the turn."""
    exit_info, records = await _run(tmp_path, mode)

    assert exit_info.returncode == 0
    assert not exit_info.cancelled
    assert not _is_running(records[0]["child_pid"])


@pytest.mark.asyncio
async def test_cancel_stops_a_cli_that_closed_its_output(tmp_path: Path) -> None:
    """Closed stdout must not prevent cancellation."""
    cancel_event = asyncio.Event()

    async def on_record(_record: dict[str, Any]) -> None:
        return None

    run = asyncio.ensure_future(
        process.run_jsonl_process(
            [sys.executable, "-c", "import os, time; os.close(1); time.sleep(60)"],
            cwd=tmp_path,
            env=os.environ,
            stdin=None,
            on_record=on_record,
            cancel_event=cancel_event,
        )
    )
    await asyncio.sleep(0.5)
    cancel_event.set()
    exit_info = await asyncio.wait_for(run, timeout=10)

    assert exit_info.cancelled
    assert exit_info.returncode is None
