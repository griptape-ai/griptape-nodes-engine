"""The engine request types whose clients don't name the workflow they act on yet.

An OPTIONAL_WORKFLOW handler gets None when its request names no workflow, except for the request
types in `ONLY_OPEN_UNTIL_CLIENTS_NAME`, whose clients don't name one yet: they get the only open
workflow, None when none is open, and fail with `WorkflowNotNamedResultFailure` when several are.
Once every client names it, the entry goes.
"""

ONLY_OPEN_UNTIL_CLIENTS_NAME: frozenset[str] = frozenset(
    {
        # The editor names no workflow yet, nor does an MCP client that sends no header and opened none.
        "CancelAllRunsRequest",
        "CancelRunRequest",
        "EnsureWorkflowAndFlowRequest",
        "ExecuteNodeRequest",
        "ExtractFlowCommandsFromImageMetadataRequest",
        "GetPublishOptionsRequest",
        "GetWorkflowContextRequest",
        "GetTopLevelFlowRequest",
        "GetWorkflowRunCommandRequest",
        "ListFlowsInFlowRequest",
        "ListRunsRequest",
        "ResolveToolApprovalsRequest",
        "RunAgentRequest",
        "RunWorkflowWithCurrentStateRequest",
        # Nodes calling from their own threads name no workflow yet.
        "BudgetAccessRequest",
        "DeclareModelInvocationRequest",
        "ReportUsageRequest",
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
