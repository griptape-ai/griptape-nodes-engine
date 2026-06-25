from dataclasses import dataclass, field

from griptape_nodes.common.macro_parser import MacroVariables
from griptape_nodes.common.project_templates.situation import BuiltInSituation
from griptape_nodes.retained_mode.events.base_events import (
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry


@dataclass
@PayloadRegistry.register
class CreateStaticFileRequest(RequestPayload):
    """Create a static file from content.

    Use when: Generating files from workflow outputs, creating downloadable content,
    storing processed data, implementing file export functionality.

    Results: CreateStaticFileResultSuccess (with URL) | CreateStaticFileResultFailure (creation error)

    Args:
        content: Content of the file base64 encoded
        file_name: Name of the file to create
    """

    content: str = field(metadata={"omit_from_result": True})
    file_name: str


@dataclass
@PayloadRegistry.register
class CreateStaticFileResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Static file created successfully.

    Args:
        url: URL where the static file can be accessed
    """

    url: str


@dataclass
@PayloadRegistry.register
class CreateStaticFileResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Static file creation failed.

    Args:
        error: Detailed error message describing the failure
    """

    error: str


@dataclass
@PayloadRegistry.register
class CreateStaticFileUploadUrlRequest(RequestPayload):
    """Create a presigned URL for uploading a static file via HTTP PUT.

    Use when: Implementing file upload functionality, allowing direct client uploads,
    enabling large file transfers, implementing drag-and-drop uploads.

    Args:
        file_name: Name of the file to be uploaded
        situation_name: Project template situation to use for resolving the upload
            path. Defaults to ``copy_external_file``. Callers that write to a
            workspace-internal location (e.g. workflow thumbnails) should pass the
            matching situation name.

    Results: CreateStaticFileUploadUrlResultSuccess (with URL and headers) | CreateStaticFileUploadUrlResultFailure (URL creation error)
    """

    file_name: str
    situation_name: str = BuiltInSituation.COPY_EXTERNAL_FILE


@dataclass
@PayloadRegistry.register
class CreateStaticFileUploadUrlResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Static file upload URL created successfully.

    Args:
        url: Presigned URL for uploading the file
        headers: HTTP headers required for the upload request
        method: HTTP method to use for upload (typically PUT)
        file_url: File URI (file://) for the absolute path where the file will be accessible after upload
    """

    url: str
    headers: dict = field(default_factory=dict)
    method: str = "PUT"
    file_url: str = ""


@dataclass
@PayloadRegistry.register
class CreateStaticFileUploadUrlResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Static file upload URL creation failed.

    Args:
        error: Detailed error message describing the failure
    """

    error: str


@dataclass
@PayloadRegistry.register
class CreateStaticFileDownloadUrlRequest(RequestPayload):
    """Create a presigned URL for downloading a static file from the staticfiles directory via HTTP GET.

    Use when: Providing secure file access to files in the staticfiles directory,
    implementing file sharing, enabling temporary download links, controlling file access permissions.

    Args:
        file_name: Name of the file to be downloaded from the staticfiles directory
        situation_name: Project template situation to use for resolving the download
            path. Defaults to ``copy_external_file`` to match the symmetric upload
            request.

    Results: CreateStaticFileDownloadUrlResultSuccess (with URL) | CreateStaticFileDownloadUrlResultFailure (URL creation error)
    """

    file_name: str
    situation_name: str = BuiltInSituation.COPY_EXTERNAL_FILE


@dataclass
@PayloadRegistry.register
class CreateStaticFileDownloadUrlFromPathRequest(RequestPayload):
    """Create a presigned URL for downloading a file from an arbitrary path.

    Use when: Need to create download URLs for files outside the staticfiles directory,
    working with absolute paths, file:// URLs, workspace-relative paths, or macro paths.

    Args:
        file_path: File path or URL. Accepts:
            - file:// URLs (e.g., "file:///absolute/path/to/file.jpg")
            - Absolute paths (e.g., "/absolute/path/to/file.jpg")
            - Workspace-relative paths (e.g., "relative/path/to/file.jpg")
            - Macro paths (e.g., "{outputs}/file.png")
        macro_variables: Optional variable substitutions for macro paths
            (e.g., {"file_name": "output", "file_ext": "png"}).
            Ignored for non-macro paths.
        preview: If True, generates and returns preview(s) rather than the original file.
            Defaults to False.

    Results: CreateStaticFileDownloadUrlResultSuccess (with URL) | CreateStaticFileDownloadUrlResultFailure (URL creation error)
    """

    file_path: str
    macro_variables: MacroVariables = field(default_factory=dict)
    preview: bool = False


@dataclass
@PayloadRegistry.register
class CreateStaticFileDownloadUrlResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Static file download URL created successfully.

    Args:
        url: Presigned URL for downloading the file
        file_url: File URI (file://) for the absolute path to the file that was used to create the download URL
    """

    url: str
    file_url: str = ""


@dataclass
@PayloadRegistry.register
class CreateStaticFileDownloadUrlFromPathResultSuccess(CreateStaticFileDownloadUrlResultSuccess):
    """Static file download URL created successfully from an arbitrary path.

    Args:
        artifact_metadata: Original properties extracted from the source file header.
            Only populated when preview=True and the file is a local image.
            Contains: width, height, format, channels, color_space, file_size.
    """

    artifact_metadata: dict | None = None


@dataclass
@PayloadRegistry.register
class CreateStaticFileDownloadUrlResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Static file download URL creation failed.

    Args:
        error: Detailed error message describing the failure
    """

    error: str
