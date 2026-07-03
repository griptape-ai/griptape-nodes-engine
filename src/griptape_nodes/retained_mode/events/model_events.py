from dataclasses import dataclass

from griptape_nodes.retained_mode.events.base_events import (
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry


@dataclass
@PayloadRegistry.register
class DownloadModelRequest(RequestPayload):
    """Download a model from Hugging Face Hub.

    Use when: Downloading models for local inference, caching models for offline use,
    retrieving specific model versions or files from Hugging Face repositories.

    Args:
        model_id: Model identifier (e.g., "microsoft/DialoGPT-medium") or full URL to Hugging Face model
        local_dir: Optional local directory to download the model to (defaults to Hugging Face cache)
        repo_type: Type of repository ("model", "dataset", or "space"). Defaults to "model"
        revision: Git revision (branch, tag, or commit hash) to download. Defaults to "main"
        allow_patterns: List of glob patterns to include when downloading. None means all files
        ignore_patterns: List of glob patterns to exclude when downloading

    Results: DownloadModelResultSuccess (with local_path) | DownloadModelResultFailure (download error)
    """

    model_id: str
    local_dir: str | None = None
    repo_type: str = "model"
    revision: str = "main"
    allow_patterns: list[str] | None = None
    ignore_patterns: list[str] | None = None


@dataclass
@PayloadRegistry.register
class DownloadModelResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Model download completed successfully.

    Args:
        model_id: The model ID that was downloaded
        repo_info: Additional repository information returned from the download
    """

    model_id: str


@dataclass
@PayloadRegistry.register
class DownloadModelResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Model download failed. Common causes: invalid model ID, network error, authentication required, storage full."""


@dataclass
class ModelInfo:
    """Information about a model."""

    model_id: str
    local_path: str | None = None
    size_bytes: int | None = None
    author: str | None = None
    downloads: int | None = None
    likes: int | None = None
    created_at: str | None = None
    updated_at: str | None = None
    task: str | None = None
    library: str | None = None
    tags: list[str] | None = None


@dataclass
class QueryInfo:
    """Information about a search query."""

    query: str | None = None
    task: str | None = None
    library: str | None = None
    author: str | None = None
    tags: list[str] | None = None
    limit: int = 20
    sort: str = "downloads"
    direction: str = "desc"


@dataclass
@PayloadRegistry.register
class ListModelsRequest(RequestPayload):
    """List all downloaded models from the local cache.

    Use when: Viewing what models are available locally, checking cache usage,
    managing local model storage.

    Results: ListModelsResultSuccess (with model list) | ListModelsResultFailure (listing error)
    """


@dataclass
@PayloadRegistry.register
class ListModelsResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Model listing completed successfully.

    Args:
        models: List of model information containing model_id, local_path, size_bytes, etc.
    """

    models: list[ModelInfo]


@dataclass
@PayloadRegistry.register
class ListModelsResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Model listing failed. Common causes: cache directory access error, filesystem error."""


@dataclass
@PayloadRegistry.register
class DeleteModelRequest(RequestPayload):
    """Delete a downloaded model from the local cache.

    Use when: Cleaning up disk space, removing unused models, managing local storage.

    Args:
        model_id: Model identifier to delete from local cache

    Results: DeleteModelResultSuccess (deletion confirmed) | DeleteModelResultFailure (deletion error)
    """

    model_id: str


@dataclass
@PayloadRegistry.register
class DeleteModelResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Model deletion completed successfully.

    Args:
        model_id: The model ID that was deleted
        deleted_path: Local path that was removed
    """

    model_id: str
    deleted_path: str


@dataclass
@PayloadRegistry.register
class DeleteModelResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Model deletion failed. Common causes: model not found, filesystem error, permission denied."""


@dataclass
@PayloadRegistry.register
class ListModelDownloadsRequest(RequestPayload):
    """List download status for a specific model or all downloads.

    Use when: Checking progress of ongoing downloads, viewing download history,
    monitoring download completion.

    Args:
        model_id: Optional model identifier to get status for. If None, returns all downloads.

    Results: ListModelDownloadsResultSuccess (with status data) | ListModelDownloadsResultFailure (query error)
    """

    model_id: str | None = None


@dataclass
class ModelDownloadStatus:
    """Model download status tracking byte-level progress."""

    model_id: str
    status: str  # "downloading", "completed", "failed"
    started_at: str
    updated_at: str
    total_bytes: int | None = None
    completed_bytes: int | None = None
    failed_bytes: int | None = None
    # Optional fields for completed downloads
    completed_at: str | None = None
    local_path: str | None = None
    # Optional fields for failed downloads
    failed_at: str | None = None
    error_message: str | None = None


@dataclass
@PayloadRegistry.register
class ListModelDownloadsResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Model download status retrieved successfully.

    Args:
        downloads: List of download status records or single status if model_id was specified
    """

    downloads: list[ModelDownloadStatus]


@dataclass
@PayloadRegistry.register
class ListModelDownloadsResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Model download status query failed. Common causes: filesystem error, invalid model ID."""


@dataclass
@PayloadRegistry.register
class DeleteModelDownloadRequest(RequestPayload):
    """Delete download status tracking records for a model.

    Use when: Cleaning up orphaned download status files, removing tracking data
    for models that are no longer needed.

    Args:
        model_id: Model identifier to remove download status for

    Results: DeleteModelDownloadResultSuccess (deletion confirmed) | DeleteModelDownloadResultFailure (deletion error)
    """

    model_id: str


@dataclass
@PayloadRegistry.register
class DeleteModelDownloadResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Model download status deletion completed successfully.

    Args:
        model_id: The model ID whose download status was deleted
        deleted_path: Path to the status file that was removed
    """

    model_id: str
    deleted_path: str


@dataclass
@PayloadRegistry.register
class DeleteModelDownloadResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Model download status deletion failed. Common causes: status not found, filesystem error, permission denied."""


@dataclass
@PayloadRegistry.register
class SearchModelsRequest(RequestPayload):
    """Search for models on Hugging Face Hub.

    Use when: Finding models by name, filtering models by task or library,
    discovering available models for specific use cases.

    Args:
        query: Search query string to match against model names and descriptions
        task: Filter by task type (e.g., "text-generation", "image-classification")
        library: Filter by library (e.g., "transformers", "diffusers", "timm")
        author: Filter by author/organization name
        tags: List of tags to filter by
        limit: Maximum number of results to return (default: 20, max: 100)
        sort: Sort results by "downloads", "likes", "updated", or "created" (default: "downloads")
        direction: Sort direction "asc" or "desc" (default: "desc")

    Results: SearchModelsResultSuccess (with model list) | SearchModelsResultFailure (search error)
    """

    query: str | None = None
    task: str | None = None
    library: str | None = None
    author: str | None = None
    tags: list[str] | None = None
    limit: int = 20
    sort: str = "downloads"
    direction: str = "desc"


@dataclass
@PayloadRegistry.register
class SearchModelsResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Model search completed successfully.

    Args:
        models: List of model information containing id, author, downloads, etc.
        total_results: Total number of models matching the search criteria
        query_info: Information about the search query parameters used
    """

    models: list[ModelInfo]
    total_results: int
    query_info: QueryInfo


@dataclass
@PayloadRegistry.register
class SearchModelsResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Model search failed. Common causes: network error, invalid parameters, API limits."""


@dataclass
@PayloadRegistry.register
class GetModelInfoRequest(RequestPayload):
    """Fetch detailed information for a specific model from Hugging Face Hub.

    Use when: Retrieving exact storage size before downloading, inspecting model
    metadata after selecting a model from search results.

    Args:
        model_id: Model identifier (e.g., "microsoft/phi-2")

    Results: GetModelInfoResultSuccess (with size and metadata) | GetModelInfoResultFailure (model not found)
    """

    model_id: str


@dataclass
@PayloadRegistry.register
class GetModelInfoResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Model info retrieved successfully.

    Args:
        model_id: The model identifier
        size_bytes: Exact storage size on Hugging Face in bytes
        safetensors_parameters: Parameter count by dtype (e.g. {"F16": 2779683840})
        author: Model author or organization
        task: Pipeline tag / task type
        library: Library name (e.g. "transformers")
        tags: List of tags
        downloads: Total download count
        likes: Total like count
    """

    model_id: str
    size_bytes: int | None
    safetensors_parameters: dict[str, int] | None
    author: str | None
    task: str | None
    library: str | None
    tags: list[str] | None
    downloads: int | None
    likes: int | None


@dataclass
@PayloadRegistry.register
class GetModelInfoResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Model info retrieval failed. Common causes: invalid model ID, network error, authentication required."""


@dataclass
@PayloadRegistry.register
class DeclareModelInvocationRequest(RequestPayload):
    """Declare that a node is about to invoke a catalog model, so the call is subject to entitlements.

    This is how a well-intentioned node opts into the permission system: before
    invoking a model it declares the invocation, and the pre-dispatch hook chain
    decides whether it is permitted. The node performs the actual inference
    itself, in its own code; this request runs no backend. A success result
    means "cleared to proceed"; a failure means the invocation is not permitted
    and the node should not run it.

    Enforcement is advisory in the sense that it relies on the node to declare.
    It is the engine-side gate the permission system sees and records every
    declared invocation through, and the natural place to meter or audit. The
    proxy independently enforces the calls that flow through it; this is the
    engine's own gate.

    The declaration identifies the model by its `model_id`: the stable catalog
    key, unique across the whole library. That key alone resolves to a single
    catalog entry, so the permission evaluator (which owns the catalog) derives
    the provider, family, and key support from it -- nothing coarser is
    declared here. Only catalog models can be declared; the stable key is the
    contract, and a concrete provider model id or `(provider, model)` pair is
    deliberately not accepted.

    Use when: A node is about to invoke a catalog model and wants the call
    gated by (and visible to) the permission system.

    Args:
        model_id: Stable catalog key of the model being invoked (e.g., "gtc_claude_opus_4_7")
        node_name: Name of the node instance declaring the invocation, when invoked from a node

    Results: DeclareModelInvocationResultSuccess (cleared to proceed) | DeclareModelInvocationResultFailure (not permitted)
    """

    model_id: str
    node_name: str | None = None


@dataclass
@PayloadRegistry.register
class DeclareModelInvocationResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """The declared model invocation is permitted; the node may proceed.

    Args:
        model_id: The stable catalog key cleared for invocation
    """

    model_id: str


@dataclass
@PayloadRegistry.register
class DeclareModelInvocationResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """The declared model invocation is not permitted. The node should not invoke the model."""
