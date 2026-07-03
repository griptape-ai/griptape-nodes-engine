"""Integration tests for orchestrator->worker secret/config propagation.

Motivation
----------
Same-machine workers share ~/.config/griptape_nodes/.env and the global config
file with the orchestrator, but each process captures values in-memory at boot
(os.environ shadow for secrets; merged_config for config). A mutation on the
orchestrator that only rewrites the file is invisible to the worker until it
re-reads the file. PR #4477 closes that gap: after orchestrator-side handlers
mutate the shared state, WorkerManager fires RefreshSecretsRequest /
ReloadConfigRequest at every registered worker, each of which re-reads
from disk.

These tests wire orchestrator-side SecretsManager/ConfigManager to a worker-
side pair via the InProcessWorkerHarness. Each test:

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
  (TestConfigReloadBroadcast / TestSecretRefreshBroadcast).
- No websocket serialization. Routing/forwarding shape is validated in
  test_harness_round_trip.py.
"""

from __future__ import annotations

import os
import platform
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from griptape_nodes.app.worker_routing import (
    RefreshSecretsRequest,
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
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def shared_global_env(tmp_path: Path) -> Iterator[Path]:
    """Patch ENV_VAR_PATH so orchestrator and worker share one on-disk .env.

    Production has them share ~/.config/griptape_nodes/.env by virtue of
    running on the same machine. Patching to a tmp path isolates the test.
    """
    env_path = tmp_path / "global.env"
    with patch("griptape_nodes.retained_mode.managers.secrets_manager.ENV_VAR_PATH", env_path):
        yield env_path


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
class TestSecretPropagation:
    """Orchestrator SetSecret should be visible to the worker after a refresh signal."""

    @pytest.mark.asyncio
    async def test_worker_holds_stale_value_until_refresh_arrives(
        self,
        shared_global_env: Path,
        shared_workspace: Path,
    ) -> None:
        """Without the refresh signal, a worker holds the boot value.

        A worker whose os.environ was set at boot keeps returning the old
        value even after the orchestrator rewrites the shared file. This
        is the motivating bug for PR #4477.
        """
        secret_key = "INTEG_STALE_SECRET"  # noqa: S105
        shared_global_env.write_text(f"{secret_key}=boot_value\n")  # noqa: ASYNC240

        # Make sure no OS-set value pre-exists. The manager's __init__ must
        # install it from the file via load_dotenv -- that is what marks the
        # key as managed and authorizes the refresh override below. A
        # pre-existing OS env var would model an operator-injected secret
        # the manager must NOT override (covered separately by
        # ``test_refresh_preserves_os_set_env_var``).
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(secret_key, None)
            harness = InProcessWorkerHarness()
            worker_config = ConfigManager()
            worker_config.workspace_path = shared_workspace
            worker_secrets = SecretsManager(worker_config, event_manager=harness.worker)
            worker_project = ProjectManager(harness.worker, worker_config, worker_secrets)
            register_broadcast_handlers(
                harness.worker,
                config_manager=worker_config,
                secrets_manager=worker_secrets,
                project_manager=worker_project,
            )

            assert worker_secrets.get_secret(secret_key) == "boot_value"

            # Someone else (the orchestrator in production) rewrites the file.
            shared_global_env.write_text(f"{secret_key}=external_update\n")  # noqa: ASYNC240

            # Without a refresh, the worker still returns the boot value because
            # os.environ is consulted first and has not been touched.
            assert worker_secrets.get_secret(secret_key) == "boot_value"

            # Now run the refresh path. This is what on_handle_worker_refresh_secrets_request
            # does on a real worker.
            await harness.start()
            try:
                await harness.route_to_worker(EventRequest(request=RefreshSecretsRequest()))
            finally:
                await harness.stop()

            assert worker_secrets.get_secret(secret_key) == "external_update"


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
                secrets_manager=worker_secrets,
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
