"""Tests for the engine discovery heartbeat handler."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import patch

from griptape_nodes.retained_mode.events.app_events import (
    EngineHeartbeatRequest,
    EngineHeartbeatResultSuccess,
)
from griptape_nodes.retained_mode.managers.os_manager import OSManager

if TYPE_CHECKING:
    import pytest

    from griptape_nodes.retained_mode.engine import Engine


class TestEngineHeartbeatOrchestratorId:
    """Heartbeat reports orchestrator_engine_id so clients can tell workers from the orchestrator.

    A worker process is spawned with GTN_ORCHESTRATOR_ENGINE_ID set to its parent's id; the
    orchestrator has no such env var. The heartbeat echoes it so a discovery client can both
    identify a worker engine and nest it under its orchestrator.
    """

    def test_orchestrator_reports_none(self, engine: Engine) -> None:
        # No GTN_ORCHESTRATOR_ENGINE_ID in the environment -> this engine IS the orchestrator.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GTN_ORCHESTRATOR_ENGINE_ID", None)
            result = engine.handle_engine_heartbeat_request(EngineHeartbeatRequest(heartbeat_id="hb-orch"))

        assert isinstance(result, EngineHeartbeatResultSuccess)
        assert result.orchestrator_engine_id is None

    def test_worker_reports_spawning_orchestrator_id(self, engine: Engine) -> None:
        # A worker process is spawned with GTN_ORCHESTRATOR_ENGINE_ID set to its parent's id;
        # the heartbeat echoes it so the client can nest this worker under that orchestrator.
        with patch.dict(os.environ, {"GTN_ORCHESTRATOR_ENGINE_ID": "eng-orchestrator"}):
            result = engine.handle_engine_heartbeat_request(EngineHeartbeatRequest(heartbeat_id="hb-worker"))

        assert isinstance(result, EngineHeartbeatResultSuccess)
        assert result.orchestrator_engine_id == "eng-orchestrator"


class TestEngineHeartbeatOperatingSystem:
    """Heartbeat reports the engine's OS so a client can tell apart engines on different machines.

    Which platform maps to which value is covered by TestPlatformName in
    tests/unit/retained_mode/managers/test_os_manager.py; this only checks that the heartbeat
    carries it.
    """

    def test_reports_the_os_it_is_running_on(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        # Patch the seam rather than sys.platform: this asserts the heartbeat carries whatever
        # OSManager reported, without telling the rest of the request it is on another OS. The
        # value is one no real platform produces, so the assertion cannot pass by coincidence on
        # a runner that happens to be that OS.
        monkeypatch.setattr(OSManager, "platform_name", staticmethod(lambda: "sentinel-os"))
        result = engine.handle_engine_heartbeat_request(EngineHeartbeatRequest(heartbeat_id="hb-os"))

        assert isinstance(result, EngineHeartbeatResultSuccess)
        assert result.engine_os == "sentinel-os"


class TestEngineHeartbeatClients:
    """Heartbeat lists the clients attached to the session so discovery can show who holds an engine."""

    def test_reports_no_clients_when_none_attached(self, engine: Engine) -> None:
        result = engine.handle_engine_heartbeat_request(EngineHeartbeatRequest(heartbeat_id="hb-none"))

        assert isinstance(result, EngineHeartbeatResultSuccess)
        assert result.clients == []

    def test_reports_an_attached_client(self, engine: Engine) -> None:
        session_manager = engine._session_manager
        session_manager.save_session("session-hb")
        assert session_manager.attach_client("nuke:shot010", "nuke")

        result = engine.handle_engine_heartbeat_request(EngineHeartbeatRequest(heartbeat_id="hb-client"))

        assert isinstance(result, EngineHeartbeatResultSuccess)
        assert [(c.client_id, c.client_type) for c in result.clients] == [("nuke:shot010", "nuke")]
