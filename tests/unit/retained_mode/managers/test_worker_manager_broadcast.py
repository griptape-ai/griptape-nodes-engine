"""Unit tests for WorkerManager.broadcast_to_workers' handling of unsendable events."""

from unittest.mock import AsyncMock

import pytest

from griptape_nodes.app.worker_routing import RefreshSecretsRequest
from griptape_nodes.retained_mode.events.base_events import EventRequest
from griptape_nodes.retained_mode.events.converters import EventSerializationError
from griptape_nodes.retained_mode.managers.worker_manager import WorkerManager, WorkerRegistration


class TestBroadcastToWorkers:
    """An event that cannot be serialized is logged instead of raising out of the broadcast."""

    @pytest.mark.asyncio
    async def test_unsendable_event_is_logged_not_raised(self, caplog: pytest.LogCaptureFixture) -> None:
        manager = WorkerManager.__new__(WorkerManager)
        manager._workers = {
            "worker-1": WorkerRegistration(request_topic="workers/1/request", worker_key=None),
            "worker-2": WorkerRegistration(request_topic="workers/2/request", worker_key=None),
        }
        manager.forward_event_to_worker = AsyncMock(side_effect=EventSerializationError("boom"))
        event = EventRequest(request=RefreshSecretsRequest())

        await manager.broadcast_to_workers(event)

        assert "Could not broadcast RefreshSecretsRequest" in caplog.text
