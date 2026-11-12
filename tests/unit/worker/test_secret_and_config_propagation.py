"""Integration tests for orchestrator->worker config propagation.

Motivation
----------
Same-machine workers share the global config file with the orchestrator, but
each process captures merged_config in memory at boot. A mutation on the
orchestrator that only rewrites the file is invisible to the worker until it
re-reads the file, so WorkerManager fires ReloadConfigRequest at every
registered worker after one. Workers read secrets from the orchestrator, so
secrets need no propagation.

These tests wire an orchestrator-side ConfigManager to a worker-side one via
the InProcessWorkerHarness. Each test:

1. Boots both managers against a shared temporary XDG config home.
2. Primes the worker's in-memory cache with a boot value.
3. Dispatches a mutation on the orchestrator's manager.
4. Delivers the refresh/reload signal through the harness (standing in for
   WorkerManager.broadcast_to_workers, which is covered by unit tests).
5. Asserts the worker now sees the fresh value.

Intentional limits
------------------
- The harness does not start a real WorkerManager/transport; the signal is
  delivered by the test directly through harness.route_to_worker. Transport-
  side fan-out is already covered in tests/unit/app/test_app_worker.py
  (TestConfigReloadBroadcast).
- No websocket serialization. Routing/forwarding shape is validated in
  test_harness_round_trip.py.
"""

from __future__ import annotations

import platform
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from griptape_nodes.app.worker_routing import (
    ReloadConfigRequest,
    register_broadcast_handlers,
)
from griptape_nodes.retained_mode.events.base_events import EventRequest
from griptape_nodes.retained_mode.events.config_events import SetConfigValueRequest
from griptape_nodes.retained_mode.managers.config_manager import ConfigManager
from griptape_nodes.retained_mode.managers.project_manager import ProjectManager
from griptape_nodes.retained_mode.managers.secrets_manager import SecretsManager
from tests.unit.worker.harness import InProcessWorkerHarness

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def shared_workspace(tmp_path: Path) -> Path:
    """A shared workspace directory both config managers anchor to."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


@pytest.mark.skipif(
    platform.system() == "Windows",
    reason="xdg_base_dirs cannot find XDG_CONFIG_HOME on Windows on GitHub Actions",
)
class TestConfigPropagation:
    """Orchestrator SetConfigValue should be visible to the worker after a reload signal."""

    @pytest.mark.asyncio
    async def test_worker_reads_new_config_after_reload(
        self,
        shared_workspace: Path,
        tmp_path: Path,
    ) -> None:
        shared_user_config = tmp_path / "griptape_nodes_config.json"
        shared_user_config.write_text('{"nested": {"key": "boot_value"}}\n')

        with patch(
            "griptape_nodes.retained_mode.managers.config_manager.USER_CONFIG_PATH",
            shared_user_config,
        ):
            harness = InProcessWorkerHarness()
            orchestrator_config = ConfigManager(event_manager=harness.orchestrator)
            orchestrator_config.workspace_path = shared_workspace
            worker_config = ConfigManager(event_manager=harness.worker)
            worker_config.workspace_path = shared_workspace
            worker_secrets = SecretsManager(worker_config, event_manager=harness.worker)
            worker_project = ProjectManager(harness.worker, worker_config, worker_secrets)
            register_broadcast_handlers(
                harness.worker,
                config_manager=worker_config,
                project_manager=worker_project,
            )

            # Both see boot value.
            assert orchestrator_config.get_config_value("nested.key") == "boot_value"
            assert worker_config.get_config_value("nested.key") == "boot_value"

            await harness.start()
            try:
                # Orchestrator writes the new value via its request handler.
                orchestrator_config.on_handle_set_config_value_request(
                    SetConfigValueRequest(category_and_key="nested.key", value="updated_value"),
                )
                assert orchestrator_config.get_config_value("nested.key") == "updated_value"

                # Worker still has the stale merged_config.
                assert worker_config.get_config_value("nested.key") == "boot_value"

                # Deliver the reload signal.
                await harness.route_to_worker(EventRequest(request=ReloadConfigRequest()))

                assert worker_config.get_config_value("nested.key") == "updated_value"
            finally:
                await harness.stop()
