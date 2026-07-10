import os
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlparse
from uuid import uuid4

from griptape.artifacts.audio_url_artifact import AudioUrlArtifact
from griptape.artifacts.error_artifact import ErrorArtifact
from griptape.artifacts.image_url_artifact import ImageUrlArtifact
from griptape.artifacts.url_artifact import UrlArtifact
from griptape.artifacts.video_url_artifact import VideoUrlArtifact

from griptape_nodes.common.parameter_hydration import hydrate_value
from griptape_nodes.drivers.storage.griptape_cloud_storage_driver import GriptapeCloudStorageDriver
from griptape_nodes.exe_types.core_types import Parameter
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.retained_mode.events.config_events import GetConfigValueRequest, GetConfigValueResultSuccess
from griptape_nodes.retained_mode.events.secrets_events import GetSecretValueRequest, GetSecretValueResultSuccess
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes


class PublicArtifactUrlParameter:
    """A reusable component for managing artifact URLs and ensuring public internet accessibility.

    This component utilizes Griptape Cloud to provide public URLs for artifact parameters if needed.
    """

    API_KEY_NAME = "GT_CLOUD_API_KEY"
    BUCKET_ID_NAME = "GT_CLOUD_BUCKET_ID"
    supported_artifact_types: ClassVar[list[type]] = [ImageUrlArtifact, VideoUrlArtifact, AudioUrlArtifact]
    supported_artifact_type_names: ClassVar[list[str]] = [cls.__name__ for cls in supported_artifact_types]
    gtc_file_path: Path | None = None

    def __init__(
        self,
        node: BaseNode,
        artifact_url_parameter: Parameter,
        disclaimer_message: str | None = None,
        request_timeout: float | None = None,
    ) -> None:
        self._node = node
        self._parameter = artifact_url_parameter
        self._disclaimer_message = disclaimer_message
        self._request_timeout = request_timeout

        if artifact_url_parameter.type.lower() not in [name.lower() for name in self.supported_artifact_type_names]:
            msg = (
                f"Unsupported artifact type '{artifact_url_parameter.type}' for "
                f"artifact URL parameter '{artifact_url_parameter.name}'. "
                f"Supported types: {', '.join(self.supported_artifact_type_names)}"
            )
            raise ValueError(msg)

        api_key = str(self._get_secret_value(self.API_KEY_NAME))
        base = os.getenv("GT_CLOUD_BASE_URL", "https://cloud.griptape.ai")
        self._storage_driver = GriptapeCloudStorageDriver(
            workspace_directory=GriptapeNodes.ConfigManager().workspace_path,
            bucket_id=self._get_bucket_id(base, api_key, timeout=self._request_timeout),
            api_key=api_key,
            base_url=base,
            request_timeout=self._request_timeout,
        )

    @classmethod
    def _get_bucket_id(cls, base_url: str, api_key: str, timeout: float | None = None) -> str:
        bucket_id: str | None = cls._get_secret_value(cls.BUCKET_ID_NAME, should_error_on_not_found=False)

        # A blank/whitespace-only secret is treated the same as an unset one: it can't
        # point at a real bucket and, left alone, produces confusing downstream 404s from
        # request URLs like `/api/buckets//assets/...`. Validate a configured ID with a
        # direct GET rather than scanning `list_buckets` -- that endpoint is paginated, so
        # a valid bucket beyond the first page would otherwise be flagged as invalid.
        if bucket_id is not None and bucket_id.strip():
            if not GriptapeCloudStorageDriver.bucket_exists(
                bucket_id,
                base_url=base_url,
                api_key=api_key,
                timeout=timeout,
            ):
                msg = (
                    f"The {cls.BUCKET_ID_NAME} secret is configured to an invalid bucket ID "
                    f"('{bucket_id}'). No Griptape Cloud storage bucket with that ID exists. "
                    f"Update the {cls.BUCKET_ID_NAME} secret to a valid bucket ID, or clear it "
                    "to auto-select a bucket."
                )
                raise RuntimeError(msg)
            return bucket_id

        # Unset or blank secret: fall back to the organization's default bucket. That
        # bucket is guaranteed to exist and cannot be deleted, so it's a stable fallback --
        # unlike auto-selecting the first entry of the paginated `list_buckets` result.
        default_bucket_id = GriptapeCloudStorageDriver.get_default_bucket_id(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
        )
        if not default_bucket_id:
            msg = (
                f"The {cls.BUCKET_ID_NAME} secret is configured to a blank bucket ID "
                "and no Griptape Cloud organization default bucket is available to fall back to. "
                f"Set the {cls.BUCKET_ID_NAME} secret to a valid bucket ID."
                if bucket_id is not None
                else "No Griptape Cloud storage buckets found!"
            )
            raise RuntimeError(msg)

        return default_bucket_id

    @classmethod
    def _get_config_value(cls, key: str, default: Any | None = None) -> Any | None:
        request = GetConfigValueRequest(category_and_key=key)
        result_event = GriptapeNodes.handle_request(request)

        if isinstance(result_event, GetConfigValueResultSuccess):
            return result_event.value

        return default

    @classmethod
    def _get_secret_value(
        cls, key: str, default: Any | None = None, *, should_error_on_not_found: bool = False
    ) -> Any | None:
        request = GetSecretValueRequest(key=key, should_error_on_not_found=should_error_on_not_found)
        result_event = GriptapeNodes.handle_request(request)

        if isinstance(result_event, GetSecretValueResultSuccess):
            return result_event.value

        return default

    def add_input_parameters(self) -> None:
        self._node.add_parameter(self._parameter)
        self._parameter.set_badge(
            variant="cloud-upload",
            title="Media Upload",
            message=self.get_help_message(),
            hide_clear_button=False,
        )

    def get_help_message(self) -> str:
        return (
            f"The {self._node.name} node requires a public URL for the parameter: {self._parameter.name}.\n\n"
            f"{self._disclaimer_message or ''}\n"
            "Executing this node will generate a short lived, public URL for the media artifact, which will be cleaned up after execution.\n"
        )

    def get_public_url_for_parameter(self) -> str:
        # Parameter values that crossed a JSON boundary (orchestrator <-> worker, workflow load)
        # arrive as serialized artifact dicts; rehydrate them back into artifacts first.
        parameter_value = hydrate_value(self._node.get_parameter_value(self._parameter.name))

        # An upstream failure propagates as an ErrorArtifact. Surface the original error
        # instead of masking it with an AttributeError further down.
        if isinstance(parameter_value, ErrorArtifact):
            msg = (
                f"Attempted to generate a public URL for parameter '{self._parameter.name}' on node "
                f"'{self._node.name}'. Failed because the upstream value is an error: {parameter_value.value}"
            )
            raise RuntimeError(msg)  # noqa: TRY004 the upstream failure is a runtime error, not a type error.

        url = parameter_value.value if isinstance(parameter_value, UrlArtifact) else parameter_value

        # check if the URL is already public
        if url.startswith(("http://", "https://")) and "localhost" not in url:
            return url

        from griptape_nodes.files.file import File

        file_contents = File(url).read_bytes()
        filename = Path(urlparse(url).path).name

        self.gtc_file_path = Path("artifact_url_storage") / uuid4().hex / filename

        # upload to Griptape Cloud and get a public URL
        public_url = self._storage_driver.upload_file(path=self.gtc_file_path, file_content=file_contents)

        return public_url

    def delete_uploaded_artifact(self) -> None:
        if not self.gtc_file_path:
            return
        self._storage_driver.delete_file(self.gtc_file_path)
