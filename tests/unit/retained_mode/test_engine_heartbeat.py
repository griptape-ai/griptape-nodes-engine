"""Tests for the engine discovery heartbeat handler."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import patch

from griptape_nodes.retained_mode.events.app_events import (
    EngineHeartbeatRequest,
    EngineHeartbeatResultSuccess,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes


class TestEngineHeartbeatOrchestratorId:
    """Heartbeat reports orchestrator_engine_id so clients can tell workers from the orchestrator.

    A worker process is spawned with GTN_ORCHESTRATOR_ENGINE_ID set to its parent's id; the
    orchestrator has no such env var. The heartbeat echoes it so a discovery client can both
    identify a worker engine and nest it under its orchestrator.
    """

    def test_orchestrator_reports_none(self, griptape_nodes: GriptapeNodes) -> None:
        # No GTN_ORCHESTRATOR_ENGINE_ID in the environment -> this engine IS the orchestrator.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GTN_ORCHESTRATOR_ENGINE_ID", None)
            result = griptape_nodes.handle_engine_heartbeat_request(EngineHeartbeatRequest(heartbeat_id="hb-orch"))

        assert isinstance(result, EngineHeartbeatResultSuccess)
        assert result.orchestrator_engine_id is None

    def test_worker_reports_spawning_orchestrator_id(self, griptape_nodes: GriptapeNodes) -> None:
        # A worker process is spawned with GTN_ORCHESTRATOR_ENGINE_ID set to its parent's id;
        # the heartbeat echoes it so the client can nest this worker under that orchestrator.
        with patch.dict(os.environ, {"GTN_ORCHESTRATOR_ENGINE_ID": "eng-orchestrator"}):
            result = griptape_nodes.handle_engine_heartbeat_request(EngineHeartbeatRequest(heartbeat_id="hb-worker"))

        assert isinstance(result, EngineHeartbeatResultSuccess)
        assert result.orchestrator_engine_id == "eng-orchestrator"
