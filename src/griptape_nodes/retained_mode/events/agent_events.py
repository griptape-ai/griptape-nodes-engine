from dataclasses import dataclass, field

from pydantic import BaseModel

from griptape_nodes.drivers.cloud_models import ProviderCatalogEntry, ProviderID
from griptape_nodes.retained_mode.events.base_events import (
    ExecutionPayload,
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    SkipTheLineMixin,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry


class PromptDriverConfig(BaseModel):
    """Typed prompt-driver fields accepted by ConfigureAgentRequest."""

    model: str | None = None
    base_url: str | None = None
    api_key_secret_name: str | None = None


class CreateProviderPayload(BaseModel):
    """Fields for CreateAgentProviderRequest. name and type are required; model defaults to empty."""

    name: str = ""
    type: str = ""
    model: str = ""
    base_url: str | None = None
    api_key_secret_name: str | None = None


class UpdateProviderPayload(BaseModel):
    """Partial fields for UpdateAgentProviderRequest. Only explicitly set fields are applied."""

    type: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key_secret_name: str | None = None


@dataclass
class RunAgentRequestArtifact:
    type: str
    value: str


@dataclass
@PayloadRegistry.register
class RunAgentRequest(RequestPayload):
    """Run an agent with input and optional artifacts.

    Use when: Executing conversational AI interactions, processing user queries,
    running autonomous agents, handling multi-modal inputs with URLs.

    Args:
        input: Text input to send to the agent
        url_artifacts: List of URL artifacts to include with the request
        thread_id: Thread ID to use for conversation.
        additional_mcp_servers: List of additional MCP server names to include

    Results: RunAgentResultStarted -> RunAgentResultSuccess (with output) | RunAgentResultFailure (execution error)
    """

    input: str
    url_artifacts: list[RunAgentRequestArtifact]
    thread_id: str
    additional_mcp_servers: list[str] = field(default_factory=list)
    provider_name: str | None = None
    model_name: str | None = None


@dataclass
@PayloadRegistry.register
class RunAgentResultStarted(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Agent execution started successfully. Execution will continue asynchronously."""


@dataclass
@PayloadRegistry.register
class RunAgentResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Agent execution completed successfully.

    Args:
        output: Dictionary containing agent response and execution results. Keys:
            ``text`` (the assistant's final text), ``message_count`` (messages in
            the thread after this turn), ``generated_image_urls`` (URLs of images
            produced by the ``generate_image`` tool this turn, in call order;
            empty when none were generated), and ``cancelled`` (``True`` when the
            run was stopped by a ``CancelAgentRequest`` before completing;
            ``text`` then holds whatever was streamed before cancellation).
        thread_id: The thread ID used for this conversation
    """

    output: dict
    thread_id: str


@dataclass
@PayloadRegistry.register
class RunAgentResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Agent execution failed.

    Args:
        error: Dictionary containing error details and failure information
    """

    error: dict


@dataclass
@PayloadRegistry.register
class CancelAgentRequest(RequestPayload, SkipTheLineMixin):
    """Cancel an in-flight agent run for a thread.

    Use when: The user stops a running chat turn. Signals cooperative
    cancellation to the active :class:`RunAgentRequest` for ``thread_id``; the
    run unwinds promptly and returns a ``RunAgentResultSuccess`` whose ``output``
    carries ``cancelled: True`` and any text streamed so far. The cancelled
    turn is not persisted to the thread.

    ``SkipTheLineMixin`` so the cancel bypasses the event queue and reaches the
    dispatcher even while the run task is in flight.

    Args:
        thread_id: ID of the thread whose active run should be cancelled.

    Results: CancelAgentResultSuccess (idempotent; succeeds even when no run is
        in flight) | CancelAgentResultFailure (cancellation could not be delivered).
    """

    thread_id: str
    broadcast_result: bool = field(default=False, kw_only=True)


@dataclass
@PayloadRegistry.register
class CancelAgentResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Cancellation was delivered.

    Args:
        thread_id: ID of the thread the cancellation targeted.
        was_running: ``True`` when a run was in flight and got signalled;
            ``False`` when no active run existed (the call is still a success).
    """

    thread_id: str
    was_running: bool


@dataclass
@PayloadRegistry.register
class CancelAgentResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Cancellation could not be delivered. Common causes: invalid thread_id."""


@dataclass
@PayloadRegistry.register
class GetConversationMemoryRequest(RequestPayload):
    """Get the agent's conversation memory.

    Use when: Reviewing conversation history, implementing memory inspection,
    debugging agent behavior, displaying conversation context.

    Args:
        thread_id: Thread ID to retrieve memory from.

    Results: GetConversationMemoryResultSuccess (with messages) | GetConversationMemoryResultFailure (memory error)
    """

    thread_id: str


@dataclass
@PayloadRegistry.register
class GetConversationMemoryResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Conversation memory retrieved successfully.

    Args:
        messages: Pydantic AI ``ModelMessage`` JSON dicts in chronological order. Each
            entry has a ``kind`` field (``"request"`` or ``"response"``) and a ``parts``
            list of typed parts (``user-prompt``, ``text``, ``tool-call``, ``tool-return``,
            etc.). Use ``pydantic_ai.messages.ModelMessagesTypeAdapter.validate_python``
            to round-trip back into typed objects.
        thread_id: The thread ID for this conversation memory.
    """

    messages: list[dict]
    thread_id: str


@dataclass
@PayloadRegistry.register
class GetConversationMemoryResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Conversation memory retrieval failed. Common causes: memory not initialized, access error."""


@dataclass
@PayloadRegistry.register
class ConfigureAgentRequest(RequestPayload):
    """Configure agent settings and behavior.

    Use when: Setting up agent parameters, changing model configurations,
    customizing agent behavior, updating agent settings.

    Args:
        prompt_driver: Dictionary of prompt driver configuration options
        image_generation_driver: Dictionary of image generation driver configuration options

    Results: ConfigureAgentResultSuccess | ConfigureAgentResultFailure (configuration error)
    """

    prompt_driver: PromptDriverConfig = field(default_factory=PromptDriverConfig)
    image_generation_driver: dict = field(default_factory=dict)
    active_provider: str = ""


@dataclass
@PayloadRegistry.register
class ConfigureAgentResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Agent configured successfully. New settings are now active."""


@dataclass
@PayloadRegistry.register
class ConfigureAgentResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Agent configuration failed. Common causes: invalid parameters, configuration error."""


@dataclass
@PayloadRegistry.register
class CreateThreadRequest(RequestPayload):
    """Create a new conversation thread.

    Use when: Starting a new conversation, initializing thread storage,
    creating named conversation contexts.

    Args:
        title: Optional title for the thread. If not provided, will be auto-generated from first message.
        local_id: Optional local identifier to store in thread metadata.

    Results: CreateThreadResultSuccess (with thread_id) | CreateThreadResultFailure (creation error)
    """

    title: str | None = None
    local_id: str | None = None


@dataclass
@PayloadRegistry.register
class CreateThreadResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Thread created successfully.

    Args:
        thread_id: Unique identifier for the created thread
        title: Thread title (may be None if not provided and no messages yet)
        created_at: ISO timestamp when thread was created
        updated_at: ISO timestamp when thread was last updated
    """

    thread_id: str
    title: str | None
    created_at: str
    updated_at: str


@dataclass
@PayloadRegistry.register
class CreateThreadResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Thread creation failed. Common causes: storage error, invalid parameters."""


@dataclass
@PayloadRegistry.register
class ListThreadsRequest(RequestPayload):
    """List all conversation threads.

    Use when: Displaying thread list, retrieving available conversations,
    implementing thread selection UI.

    Results: ListThreadsResultSuccess (with threads) | ListThreadsResultFailure (retrieval error)
    """


@dataclass
class ThreadMetadata:
    """Metadata for a conversation thread."""

    thread_id: str
    title: str | None
    created_at: str
    updated_at: str
    message_count: int
    archived: bool
    local_id: str | None = None


@dataclass
@PayloadRegistry.register
class ListThreadsResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Threads retrieved successfully.

    Args:
        threads: List of thread metadata objects
    """

    threads: list[ThreadMetadata]


@dataclass
@PayloadRegistry.register
class ListThreadsResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Thread listing failed. Common causes: storage error, permission error."""


@dataclass
@PayloadRegistry.register
class DeleteThreadRequest(RequestPayload):
    """Delete a conversation thread permanently.

    Use when: Removing unwanted conversations, cleaning up storage,
    implementing thread deletion UI.

    Args:
        thread_id: ID of the thread to delete

    Results: DeleteThreadResultSuccess | DeleteThreadResultFailure (deletion error)
    """

    thread_id: str


@dataclass
@PayloadRegistry.register
class DeleteThreadResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Thread deleted successfully.

    Args:
        thread_id: ID of the deleted thread
    """

    thread_id: str


@dataclass
@PayloadRegistry.register
class DeleteThreadResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Thread deletion failed. Common causes: thread not found, storage error."""


@dataclass
@PayloadRegistry.register
class RenameThreadRequest(RequestPayload):
    """Rename an existing thread.

    Use when: Updating thread titles, organizing conversations,
    implementing thread editing UI.

    Args:
        thread_id: ID of the thread to rename
        new_title: New title for the thread

    Results: RenameThreadResultSuccess | RenameThreadResultFailure (rename error)
    """

    thread_id: str
    new_title: str


@dataclass
@PayloadRegistry.register
class RenameThreadResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Thread renamed successfully.

    Args:
        thread_id: ID of the renamed thread
        title: New title of the thread
        updated_at: ISO timestamp when thread was updated
    """

    thread_id: str
    title: str
    updated_at: str


@dataclass
@PayloadRegistry.register
class RenameThreadResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Thread rename failed. Common causes: thread not found, storage error."""


@dataclass
@PayloadRegistry.register
class ArchiveThreadRequest(RequestPayload):
    """Archive a conversation thread.

    Use when: Organizing conversations, hiding inactive threads,
    cleaning up thread list without permanently deleting.

    Args:
        thread_id: ID of the thread to archive

    Results: ArchiveThreadResultSuccess | ArchiveThreadResultFailure (archive error)
    """

    thread_id: str


@dataclass
@PayloadRegistry.register
class ArchiveThreadResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Thread archived successfully.

    Args:
        thread_id: ID of the archived thread
        updated_at: ISO timestamp when thread was updated
    """

    thread_id: str
    updated_at: str


@dataclass
@PayloadRegistry.register
class ArchiveThreadResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Thread archive failed. Common causes: thread not found, already archived, storage error."""


@dataclass
@PayloadRegistry.register
class UnarchiveThreadRequest(RequestPayload):
    """Unarchive a conversation thread.

    Use when: Restoring archived conversations, resuming old threads,
    making archived threads active again.

    Args:
        thread_id: ID of the thread to unarchive

    Results: UnarchiveThreadResultSuccess | UnarchiveThreadResultFailure (unarchive error)
    """

    thread_id: str


@dataclass
@PayloadRegistry.register
class UnarchiveThreadResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Thread unarchived successfully.

    Args:
        thread_id: ID of the unarchived thread
        updated_at: ISO timestamp when thread was updated
    """

    thread_id: str
    updated_at: str


@dataclass
@PayloadRegistry.register
class UnarchiveThreadResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Thread unarchive failed. Common causes: thread not found, not archived, storage error."""


@dataclass
@PayloadRegistry.register
class AgentStreamEvent(ExecutionPayload):
    """Streaming token event during agent execution.

    Use when: Implementing real-time agent output, displaying progressive responses,
    building streaming UIs, monitoring agent token generation.

    Args:
        token: Individual token generated by the agent during execution
    """

    token: str


@dataclass
@PayloadRegistry.register
class AgentToolCallEvent(ExecutionPayload):
    """Agent invoked a tool. Emitted when the model commits a tool call before execution.

    Use when: Rendering tool-call cards in chat UIs, surfacing in-flight tool work,
    debugging multi-step agent runs.

    Args:
        tool_call_id: Stable identifier for this call. Pairs with the matching
            ``AgentToolResultEvent.tool_call_id``.
        tool_name: Name of the tool the agent invoked.
        args: JSON-encoded preview of the tool arguments. May be ``"{}"`` when the
            model produced no arguments.
    """

    tool_call_id: str
    tool_name: str
    args: str


@dataclass
@PayloadRegistry.register
class AgentToolResultEvent(ExecutionPayload):
    """Tool call returned. Emitted after the workspace or MCP tool produces a result.

    Use when: Updating tool-call cards with output, showing tool errors inline,
    chaining UI state off the agent's tool pipeline.

    Args:
        tool_call_id: Identifier matching the originating ``AgentToolCallEvent``.
        tool_name: Name of the tool that produced this result.
        content: Stringified tool output. Non-string returns are JSON-encoded.
        is_error: ``True`` when the tool raised or returned a retry prompt.
    """

    tool_call_id: str
    tool_name: str
    content: str
    is_error: bool = False


@dataclass
@PayloadRegistry.register
class AgentThinkingEvent(ExecutionPayload):
    """Streaming reasoning/thinking delta from the underlying model.

    Use when: Showing a "thinking\u2026" indicator or rendering reasoning content
    parts as the agent works through a problem.

    Args:
        delta: Incremental thinking text. Concatenate successive deltas to assemble
            the full reasoning trace for a turn.
    """

    delta: str


@dataclass
@PayloadRegistry.register
class ListAgentModelsRequest(RequestPayload):
    """List the prompt and image models available to the chat sidebar agent.

    Use when: Populating the chat sidebar's model-picker dropdowns. The
    engine-bundled agent is wired to Griptape Cloud (`GriptapeCloudPromptDriver`,
    `GriptapeCloudImageGenerationDriver`), so this returns the GTC catalog.

    Results: ListAgentModelsResultSuccess (with model lists and deprecation map) |
        ListAgentModelsResultFailure (engine error).
    """


@dataclass
@PayloadRegistry.register
class ListAgentModelsResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Agent model lists retrieved successfully.

    Args:
        prompt_models: Ordered list of prompt-model IDs available on Griptape Cloud.
        image_models: Ordered list of image-model IDs available on Griptape Cloud.
        deprecated_models: Mapping of deprecated model ID to live replacement
            (covers both the prompt and image namespaces).
        providers: Ordered list of provider catalog entries. Each entry has:
            ``id``, ``display_name``, ``terms_url`` (str or None),
            ``key_support`` (str or None), ``notes`` (str or None),
            ``requires_api_key`` (bool convenience field),
            ``default_base_url`` (str or None), ``has_model_list`` (bool),
            ``default_model`` (str).
    """

    prompt_models: list[str] = field(default_factory=list)
    image_models: list[str] = field(default_factory=list)
    deprecated_models: dict[str, str] = field(default_factory=dict)
    providers: list[ProviderCatalogEntry] = field(default_factory=list)


@dataclass
@PayloadRegistry.register
class ListAgentModelsResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Agent model list retrieval failed."""


@dataclass
@PayloadRegistry.register
class ListProviderModelsRequest(RequestPayload):
    """List models available from a specific provider endpoint.

    For ``griptape_cloud`` returns the static curated catalog. For all other
    providers makes a ``GET {base_url}/models`` call (OpenAI-compatible) and
    returns whatever models the server reports. Use this to populate a model
    picker when the user has selected a non-Griptape-Cloud provider.

    Args:
        provider: Provider id — ``"griptape_cloud"``, ``"ollama"``, ``"lmstudio"``, or ``"custom"``.
        base_url: Base URL of the endpoint (required for non-Griptape-Cloud providers).
        api_key: API key sent as ``Authorization: Bearer`` (optional; omit for Ollama).

    Results: ListProviderModelsResultSuccess | ListProviderModelsResultFailure
    """

    provider: str = ProviderID.GRIPTAPE_CLOUD
    base_url: str = ""
    api_key: str = ""


@dataclass
@PayloadRegistry.register
class ListProviderModelsResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Provider model list retrieved successfully.

    Args:
        models: Ordered list of model IDs reported by the provider.
    """

    models: list[str] = field(default_factory=list)


@dataclass
@PayloadRegistry.register
class ListProviderModelsResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Provider model list retrieval failed. Common causes: provider unreachable, bad URL."""


@dataclass
@PayloadRegistry.register
class GetAgentConfigRequest(RequestPayload):
    """Get the current agent configuration.

    Use when: Populating the agent settings panel with the current provider,
    model, and endpoint values so the UI reflects live engine state.

    Results: GetAgentConfigResultSuccess | GetAgentConfigResultFailure
    """


@dataclass
@PayloadRegistry.register
class GetAgentConfigResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Current agent configuration retrieved successfully.

    Args:
        provider: Active provider type id (e.g. ``"griptape_cloud"``, ``"ollama"``, ``"lmstudio"``, ``"custom"``).
        active_provider: ``name`` of the currently active provider. Use this (not ``provider``)
            to round-trip provider identity, since multiple providers can share the same type.
        model_name: Active prompt model id.
        image_model_name: Active image generation model id.
        base_url: Custom base URL in use for non-Griptape-Cloud providers.
            Empty string when the provider manages its own URL.
    """

    provider: str
    active_provider: str
    model_name: str
    image_model_name: str
    base_url: str


@dataclass
@PayloadRegistry.register
class GetAgentConfigResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Agent config retrieval failed."""


@dataclass
@PayloadRegistry.register
class ListAgentProvidersRequest(RequestPayload):
    """List all configured agent providers.

    Use when: Populating the provider management UI, letting users see and
    enable/disable named agent provider configurations.

    Results: ListAgentProvidersResultSuccess | ListAgentProvidersResultFailure
    """


@dataclass
@PayloadRegistry.register
class ListAgentProvidersResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Agent provider list retrieved successfully.

    Args:
        providers: Ordered list of agent provider config dicts. Each has at
            minimum ``name``, ``type``, and ``model``. Optional keys the engine
            stores and returns verbatim (no enforcement):

            ``base_url`` — endpoint URL for non-Griptape-Cloud providers.
            ``api_key_secret_name`` — name of a secret in the SecretsManager
                whose value is used as the API key at runtime (``custom`` only).
            ``enabled`` — frontend toggle; when ``false`` the UI may hide or
                disable the provider in the picker. The engine does not block
                ``SetActiveProviderRequest`` based on this value.
            ``icon`` — URL or data-URI of an image to show next to the provider
                name in the UI. User-supplied for custom providers; the engine
                does not validate or resize it.
            ``description``, ``docs_url``, ``download_url`` — free-form metadata
                the UI may surface in detail/hover views.

            ``griptape_cloud`` is always the first entry and cannot be deleted.
        active_provider: ``name`` of the currently active provider.
    """

    providers: list[dict] = field(default_factory=list)
    active_provider: str = ""


@dataclass
@PayloadRegistry.register
class ListAgentProvidersResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Agent provider list retrieval failed."""


@dataclass
@PayloadRegistry.register
class CreateAgentProviderRequest(RequestPayload):
    """Create a new named agent provider configuration.

    Use when: Adding a new agent provider (Ollama instance, custom endpoint,
    etc.) to the list of available agent providers.

    Args:
        provider: Provider config. Required fields: ``name`` (unique,
            non-empty), ``type`` (one of the known preset ids). Optional:
            ``model``, ``base_url``, ``api_key_secret_name``.

    Results: CreateAgentProviderResultSuccess | CreateAgentProviderResultFailure
    """

    provider: CreateProviderPayload = field(default_factory=CreateProviderPayload)


@dataclass
@PayloadRegistry.register
class CreateAgentProviderResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Agent provider created successfully.

    Args:
        name: ``name`` of the newly created agent provider.
    """

    name: str = ""


@dataclass
@PayloadRegistry.register
class CreateAgentProviderResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Agent provider creation failed. Common causes: duplicate name, missing required fields, unknown type."""


@dataclass
@PayloadRegistry.register
class UpdateAgentProviderRequest(RequestPayload):
    """Update an existing named agent provider configuration.

    Use when: Editing an agent provider's model, base URL, API key, or metadata.

    Args:
        name: ``name`` of the provider to update (must already exist).
        provider: Partial provider config. Only explicitly set fields are applied;
            omitted fields are preserved. Rename is not supported.

    Results: UpdateAgentProviderResultSuccess | UpdateAgentProviderResultFailure
    """

    name: str = ""
    provider: UpdateProviderPayload = field(default_factory=UpdateProviderPayload)


@dataclass
@PayloadRegistry.register
class UpdateAgentProviderResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Agent provider updated successfully."""


@dataclass
@PayloadRegistry.register
class UpdateAgentProviderResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Agent provider update failed. Common causes: provider not found, unknown type."""


@dataclass
@PayloadRegistry.register
class DeleteAgentProviderRequest(RequestPayload):
    """Delete a named agent provider configuration.

    The ``griptape_cloud`` provider cannot be deleted.

    Use when: Removing a agent provider the user no longer needs.

    Args:
        name: ``name`` of the provider to delete.

    Results: DeleteAgentProviderResultSuccess | DeleteAgentProviderResultFailure
    """

    name: str = ""


@dataclass
@PayloadRegistry.register
class DeleteAgentProviderResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Agent provider deleted successfully.

    Args:
        name: ``name`` of the deleted agent provider.
    """

    name: str = ""


@dataclass
@PayloadRegistry.register
class DeleteAgentProviderResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Agent provider deletion failed. Common causes: provider not found, protected provider, last remaining provider."""
