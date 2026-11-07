from dataclasses import dataclass

from griptape_nodes.retained_mode.events.base_events import (
    AppPayload,
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry


@dataclass
@PayloadRegistry.register
class StartSyncAllCloudWorkflowsRequest(
    RequestPayload["StartSyncAllCloudWorkflowsResultSuccess", "StartSyncAllCloudWorkflowsResultFailure"]
):
    """Start syncing all cloud workflows to local synced_workflows directory.

    Use when: Initiating download of all workflow files from cloud storage, keeping local sync directory updated,
    preparing for offline workflow development, backing up cloud workflows locally.

    Results: StartSyncAllCloudWorkflowsResultSuccess (sync started) | StartSyncAllCloudWorkflowsResultFailure (failed to start)
    """


@dataclass
@PayloadRegistry.register
class StartSyncAllCloudWorkflowsResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Cloud workflow sync started successfully.

    Args:
        sync_directory: Path to the local sync directory where files will be saved
        total_workflows: Number of workflows that will be synced
    """

    sync_directory: str
    total_workflows: int


@dataclass
@PayloadRegistry.register
class StartSyncAllCloudWorkflowsResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Cloud workflow sync failed to start. Common causes: cloud not configured, network error, storage error, permission denied."""


@dataclass
@PayloadRegistry.register
class SyncComplete(AppPayload):
    """Cloud workflow sync completed successfully.

    Args:
        sync_directory: Path to the local sync directory where files were saved
        synced_workflows: List of workflows that were successfully synced
        failed_workflows: List of workflows that failed to sync
        total_workflows: Total number of workflows that were processed
    """

    sync_directory: str
    synced_workflows: list[str]
    failed_workflows: list[str]
    total_workflows: int
