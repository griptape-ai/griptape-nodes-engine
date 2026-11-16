"""A real worker process this engine starts for itself, on its channel, end to end.

A library's worker is started the way the engine starts every local worker:
`python -m griptape_nodes.worker --library-name NAME`, a child process on a private channel. It boots an
engine of its own, loads its library from the config it shares with this engine, and says hello. Everything it asks for while booting reaches this
engine: its secrets pass this engine's worker request gate as the wire envelope a client's request is,
and its library load's checkpoints are decided by this engine's authorization hook.

No sleeps: every wait is on the worker's own replies, bounded by timeouts generous enough for a cold
interpreter on a loaded Windows CI box.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from griptape_nodes.retained_mode.engine import current_engine
from griptape_nodes.retained_mode.events.app_events import GetEngineVersionRequest, GetEngineVersionResultSuccess
from griptape_nodes.retained_mode.events.base_events import EventRequest
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.managers.authorization_checkpoint import AuthorizationCheckpoint, CheckpointAction
from griptape_nodes.retained_mode.managers.event_manager import WorkerRequestGateDecision
from griptape_nodes.utils.engine_dirs import CONFIG_DIR_ENV_VAR, DATA_DIR_ENV_VAR, STATE_DIR_ENV_VAR

if TYPE_CHECKING:
    from collections.abc import Callable

_FIXTURES = Path(__file__).parent / "fixtures" / "echo_library"
_LIBRARY_NAME = "Echo Library"
# Starting a Python process and building an engine in it, on the slowest CI runner.
_BOOT_TIMEOUT_S = 300.0


@pytest.mark.worker_processes
@pytest.mark.asyncio
async def test_a_local_worker_runs_as_this_engines_child_and_asks_it_for_everything(
    materialize_library: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker boots, says hello, answers a request, and asked this engine for its secrets and checkpoints."""
    library_json = materialize_library(
        tmp_path / "echo_library",
        template=_FIXTURES / "griptape_nodes_library.json",
        node_file=_FIXTURES / "echo_node.py",
    )
    # The worker inherits this process's environment, so these keep it off the developer's own
    # config, libraries, and logs.
    for variable, name in ((CONFIG_DIR_ENV_VAR, "config"), (DATA_DIR_ENV_VAR, "data"), (STATE_DIR_ENV_VAR, "state")):
        (tmp_path / name).mkdir()
        monkeypatch.setenv(variable, str(tmp_path / name))
    (tmp_path / "config" / "griptape_nodes_config.json").write_text(
        json.dumps(
            {
                "workspace_directory": str(tmp_path / "workspace"),
                "log_level": "WARNING",
                "app_events": {"on_app_initialization_complete": {"libraries_to_register": [str(library_json)]}},
            }
        )
    )

    engine = current_engine()
    registered = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(registered, RegisterLibraryFromFileResultSuccess), registered.result_details
    gated: list[dict] = []
    checkpoints: list[AuthorizationCheckpoint] = []

    def record_request(envelope: dict) -> WorkerRequestGateDecision:
        gated.append(envelope)
        return WorkerRequestGateDecision(forward=envelope, denials=[])

    def record_checkpoint(checkpoint: AuthorizationCheckpoint) -> None:
        checkpoints.append(checkpoint)

    engine.event_manager.add_worker_request_gate(record_request)
    engine.event_manager.add_authorization_hook(record_checkpoint)
    worker_manager = engine.worker_manager
    # No static server runs in a test; waiting for one to be decided would only delay the spawn.
    monkeypatch.setattr(worker_manager, "_orchestrator_static_server_base_url", AsyncMock(return_value=None))
    worker_manager.library_load_timeout_s = _BOOT_TIMEOUT_S
    worker_manager.expect_worker(_LIBRARY_NAME)

    try:
        await asyncio.wait_for(worker_manager._spawn_library_worker(_LIBRARY_NAME), _BOOT_TIMEOUT_S)
        await worker_manager.wait_until_executable(_LIBRARY_NAME)
        assert worker_manager.worker_unavailable_reason(_LIBRARY_NAME) is None
        worker_engine_id = worker_manager.get_worker_for_key(_LIBRARY_NAME)
        assert worker_engine_id is not None
        raw = await asyncio.wait_for(
            worker_manager.route_to_worker(EventRequest(request=GetEngineVersionRequest()), worker_engine_id),
            _BOOT_TIMEOUT_S,
        )
    finally:
        await worker_manager.reset_workers()

    assert raw["result_type"] == GetEngineVersionResultSuccess.__name__
    # Its engine reads every secret from this one, through the gate this engine screens requests with.
    assert "GetSecretValueRequest" in {envelope["payload"]["request_type"] for envelope in gated}
    assert all(envelope["type"] == "EventRequest" for envelope in gated)
    # Its library load asked this engine whether the library may load.
    assert any(
        checkpoint.action is CheckpointAction.LOAD_LIBRARY and checkpoint.subject_id == _LIBRARY_NAME
        for checkpoint in checkpoints
    )
