"""The engine request types whose handlers still rely on the workflow their clients don't name.

An engine handler with no `workflow` parameter is HOST (see `RequestScope`), except for the request
types in `AMBIENT_UNTIL_MIGRATED`, which act on the caller's workflow, else the only open one.
Migrating a handler, by giving it a `workflow` parameter or confirming it is HOST, removes its entry.

An OPTIONAL_WORKFLOW handler gets None when its request names no workflow, except for the request
types in `ONLY_OPEN_UNTIL_CLIENTS_NAME`, whose clients don't name one yet: they get the only open
workflow, None when none is open, and fail with `WorkflowNotNamedResultFailure` when several are.
Once every client names it, the entry goes.
"""

ONLY_OPEN_UNTIL_CLIENTS_NAME: frozenset[str] = frozenset(
    {
        # The editor and the MCP server name no workflow yet.
        "EnsureWorkflowAndFlowRequest",
        "ExecuteNodeRequest",
        "GetPublishOptionsRequest",
        "GetWorkflowRunCommandRequest",
        # Paths, macros and file provenance resolve {workflow_dir} for the open workflow.
        "AttemptMapAbsolutePathToProjectRequest",
        "AttemptMatchPathAgainstMacroRequest",
        "CreateStaticFileDownloadUrlFromPathRequest",
        "CreateStaticFileDownloadUrlRequest",
        "CreateStaticFileRequest",
        "CreateStaticFileUploadUrlRequest",
        "GeneratePreviewFromDefaultsRequest",
        "GeneratePreviewRequest",
        "GetNextVersionIndexRequest",
        "GetPathForMacroRequest",
        "GetPreviewForArtifactRequest",
        "GetStateForMacroRequest",
        "ListDirectoryRequest",
        "ListDirectorySequencesRequest",
        "ResolveMacroPathRequest",
        "ScanSequencesRequest",
        "WriteFileRequest",
        "WriteTempFileRequest",
    }
)

AMBIENT_UNTIL_MIGRATED: frozenset[str] = frozenset(
    {
        "GetWorkflowContextRequest",
        "RunWorkflowFromRegistryRequest",
        "RunWorkflowFromScratchRequest",
        "RunWorkflowWithCurrentStateRequest",
        "SetWorkflowContextRequest",
    }
)
