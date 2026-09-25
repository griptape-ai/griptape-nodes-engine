from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import threading
from functools import partial
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse
from uuid import uuid4

import httpx2
from griptape.artifacts.audio_url_artifact import AudioUrlArtifact
from griptape.artifacts.error_artifact import ErrorArtifact
from griptape.artifacts.image_url_artifact import ImageUrlArtifact
from griptape.artifacts.url_artifact import UrlArtifact
from griptape.artifacts.video_url_artifact import VideoUrlArtifact

from griptape_nodes.drivers.cloud_credentials import MISSING_CREDENTIAL_MESSAGE, resolve_cloud_credential
from griptape_nodes.drivers.storage.griptape_cloud_storage_driver import GriptapeCloudStorageDriver
from griptape_nodes.files.file import File
from griptape_nodes.retained_mode.events.config_events import GetConfigValueRequest, GetConfigValueResultSuccess
from griptape_nodes.retained_mode.events.secrets_events import GetSecretValueRequest, GetSecretValueResultSuccess

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.exe_types.core_types import Parameter
    from griptape_nodes.exe_types.node_types import BaseNode
    from griptape_nodes.retained_mode.engine import Engine

logger = logging.getLogger("griptape_nodes")


class PublicArtifactUrlParameter:
    """A reusable component for managing artifact URLs and ensuring public internet accessibility.

    This component utilizes Griptape Cloud to provide public URLs for artifact parameters if needed.
    """

    API_KEY_NAME = "GT_CLOUD_API_KEY"
    BUCKET_ID_NAME = "GT_CLOUD_BUCKET_ID"
    supported_artifact_types: ClassVar[list[type]] = [ImageUrlArtifact, VideoUrlArtifact, AudioUrlArtifact]
    supported_artifact_type_names: ClassVar[list[str]] = [cls.__name__ for cls in supported_artifact_types]
    # Process-wide on purpose: keys carry the base URL and credential, so engines never share an entry.
    # Bucket lookup requires a network round trip; cache results across helper instances.
    _bucket_id_cache: ClassVar[dict[tuple[str, str, str | None], str]] = {}
    # One lock per cache key, so a slow lookup does not hold up other configurations.
    _bucket_id_locks: ClassVar[dict[tuple[str, str, str | None], threading.Lock]] = {}
    _bucket_id_locks_guard: ClassVar[threading.Lock] = threading.Lock()
    # Keep work outliving a cancelled run alive until completion.
    _background_tasks: ClassVar[set[asyncio.Future[Any]]] = set()

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
        self.gtc_file_path: Path | None = None

        if artifact_url_parameter.type.lower() not in [name.lower() for name in self.supported_artifact_type_names]:
            msg = (
                f"Unsupported artifact type '{artifact_url_parameter.type}' for "
                f"artifact URL parameter '{artifact_url_parameter.name}'. "
                f"Supported types: {', '.join(self.supported_artifact_type_names)}"
            )
            raise ValueError(msg)

        api_key = resolve_cloud_credential(node.engine.secrets_manager, secret_name=self.API_KEY_NAME)
        if not api_key:
            msg = (
                f"Attempted to make '{artifact_url_parameter.name}' publicly accessible. "
                f"Failed because {MISSING_CREDENTIAL_MESSAGE}"
            )
            raise ValueError(msg)

        self._api_key = api_key
        self._base_url = os.getenv("GT_CLOUD_BASE_URL", "https://cloud.griptape.ai")
        # Building the driver resolves the bucket over the network.
        self._storage_driver: GriptapeCloudStorageDriver | None = None

    @classmethod
    def _get_bucket_id(cls, engine: Engine, base_url: str, api_key: str, timeout: float | None = None) -> str:
        return cls._resolve_bucket_id(cls._read_configured_bucket_id(engine), base_url, api_key, timeout=timeout)

    @classmethod
    def _read_configured_bucket_id(cls, engine: Engine) -> str | None:
        # Engine requests must run on the event loop.
        return cls._get_secret_value(engine, cls.BUCKET_ID_NAME, should_error_on_not_found=False)

    @classmethod
    def _resolve_bucket_id(
        cls, configured_bucket_id: str | None, base_url: str, api_key: str, timeout: float | None = None
    ) -> str:
        """Safe to run in a worker thread: makes no engine requests."""
        cache_key = (base_url, api_key, configured_bucket_id)
        cached = cls._bucket_id_cache.get(cache_key)
        if cached is not None:
            return cached
        with cls._bucket_id_locks_guard:
            key_lock = cls._bucket_id_locks.setdefault(cache_key, threading.Lock())
        # Held across the lookup so concurrent first uploads share one result.
        with key_lock:
            cached = cls._bucket_id_cache.get(cache_key)
            if cached is not None:
                return cached
            bucket_id = cls._lookup_bucket_id(configured_bucket_id, base_url, api_key, timeout=timeout)
            cls._bucket_id_cache[cache_key] = bucket_id
            return bucket_id

    @classmethod
    def _lookup_bucket_id(cls, bucket_id: str | None, base_url: str, api_key: str, timeout: float | None = None) -> str:
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
    def _get_config_value(cls, engine: Engine, key: str, default: Any | None = None) -> Any | None:
        request = GetConfigValueRequest(category_and_key=key)
        result_event = engine.handle_request(request)

        if isinstance(result_event, GetConfigValueResultSuccess):
            return result_event.value

        return default

    @classmethod
    def _get_secret_value(
        cls, engine: Engine, key: str, default: Any | None = None, *, should_error_on_not_found: bool = False
    ) -> Any | None:
        request = GetSecretValueRequest(key=key, should_error_on_not_found=should_error_on_not_found)
        result_event = engine.handle_request(request)

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
        url = self._get_url_to_publish()
        if self._is_public(url):
            return url

        file_contents = File(url).read_bytes()
        # Resolved before recording the path, so a failed lookup leaves nothing for cleanup to delete.
        driver = self._get_storage_driver()
        self.gtc_file_path = self._build_upload_path(url)
        try:
            return driver.upload_file(path=self.gtc_file_path, file_content=file_contents)
        except RuntimeError as e:
            self._forget_storage_driver_if_bucket_missing(driver, e)
            raise

    async def aget_public_url_for_parameter(self) -> str:
        """Resolve parameter values on the event loop, where node project context is available."""
        url = self._get_url_to_publish()
        if self._is_public(url):
            return url

        file_contents = await File(url).aread_bytes()
        driver = await self._aget_storage_driver()
        upload_path = self._build_upload_path(url)
        self.gtc_file_path = upload_path
        try:
            return await self._run_off_loop(
                partial(driver.upload_file, path=upload_path, file_content=file_contents),
                after_cancelled_call=partial(driver.delete_file, upload_path),
            )
        except asyncio.CancelledError:
            # The background cleanup owns the delete now.
            self._take_upload_path()
            raise
        except RuntimeError as e:
            self._forget_storage_driver_if_bucket_missing(driver, e)
            raise

    def delete_uploaded_artifact(self) -> None:
        path = self._take_upload_path()
        if path is None:
            return
        self._get_storage_driver().delete_file(path)

    async def adelete_uploaded_artifact(self) -> None:
        if self.gtc_file_path is None:
            return
        # Resolved before taking the path, so a cancel during resolution leaves the path recorded.
        driver = await self._aget_storage_driver()
        path = self._take_upload_path()
        if path is None:
            return
        await self._run_off_loop(partial(driver.delete_file, path))

    def _get_url_to_publish(self) -> str:
        # A helper instance lives as long as the node, so an upload path recorded by an
        # earlier run is cleared before anything else: it would otherwise be re-deleted by
        # delete_uploaded_artifact, and callers read gtc_file_path to tell an upload from
        # the already-public pass-through.
        self.gtc_file_path = None

        parameter_value = self._node.get_parameter_value(self._parameter.name)

        # An upstream failure propagates as an ErrorArtifact. Surface the original error
        # instead of masking it with an AttributeError further down.
        if isinstance(parameter_value, ErrorArtifact):
            msg = (
                f"Attempted to generate a public URL for parameter '{self._parameter.name}' on node "
                f"'{self._node.name}'. Failed because the upstream value is an error: {parameter_value.value}"
            )
            raise RuntimeError(msg)  # noqa: TRY004 the upstream failure is a runtime error, not a type error.

        if isinstance(parameter_value, UrlArtifact):
            return parameter_value.value
        if isinstance(parameter_value, dict):
            # The editor sets artifact parameters to artifact-shaped dicts.
            return parameter_value.get("value")
        return parameter_value

    def _build_upload_path(self, url: str) -> Path:
        return Path("artifact_url_storage") / uuid4().hex / self._derive_upload_filename(url)

    def _take_upload_path(self) -> Path | None:
        # Clear before deletion to prevent concurrent cleanup from deleting twice.
        path = self.gtc_file_path
        self.gtc_file_path = None
        return path

    def _get_storage_driver(self) -> GriptapeCloudStorageDriver:
        if self._storage_driver is not None:
            return self._storage_driver
        bucket_id = self._get_bucket_id(self._node.engine, self._base_url, self._api_key, timeout=self._request_timeout)
        return self._build_storage_driver(bucket_id)

    async def _aget_storage_driver(self) -> GriptapeCloudStorageDriver:
        if self._storage_driver is not None:
            return self._storage_driver
        configured_bucket_id = self._read_configured_bucket_id(self._node.engine)
        cache_key = (self._base_url, self._api_key, configured_bucket_id)
        bucket_id = self._bucket_id_cache.get(cache_key)
        if bucket_id is None:
            bucket_id = await self._run_off_loop(
                partial(
                    self._resolve_bucket_id,
                    configured_bucket_id,
                    self._base_url,
                    self._api_key,
                    timeout=self._request_timeout,
                )
            )
        return self._build_storage_driver(bucket_id)

    def _forget_storage_driver_if_bucket_missing(self, driver: GriptapeCloudStorageDriver, error: Exception) -> None:
        """After a 404, drop the cached bucket so the next upload revalidates it.

        A configured bucket deleted mid-session would otherwise fail every later upload with a
        bare 404 instead of the invalid-bucket error. Transient failures keep the cache.
        """
        if not self._is_not_found(error):
            return
        # Nothing was stored in a bucket that is gone, and cleanup would only re-resolve it and fail.
        self._take_upload_path()
        if self._storage_driver is driver:
            self._storage_driver = None
        for cache_key, bucket_id in list(self._bucket_id_cache.items()):
            if cache_key[:2] == (self._base_url, self._api_key) and bucket_id == driver.bucket_id:
                self._bucket_id_cache.pop(cache_key, None)

    def _build_storage_driver(self, bucket_id: str) -> GriptapeCloudStorageDriver:
        self._storage_driver = GriptapeCloudStorageDriver(
            self._node.engine.config_manager,
            bucket_id=bucket_id,
            api_key=self._api_key,
            base_url=self._base_url,
            request_timeout=self._request_timeout,
        )
        return self._storage_driver

    @classmethod
    async def _run_off_loop[T](
        cls, call: Callable[[], T], *, after_cancelled_call: Callable[[], Any] | None = None
    ) -> T:
        """Run a blocking call in a thread without making a cancelled caller wait for it.

        The thread cannot be interrupted, so a cancelled call finishes in the background, followed
        by `after_cancelled_call` whether it succeeded or not.
        """
        task = asyncio.ensure_future(asyncio.to_thread(call))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cls._finish_in_background(task, then=after_cancelled_call)
            raise

    @classmethod
    def _finish_in_background(
        cls,
        task: asyncio.Future[Any],
        *,
        then: Callable[[], Any] | None = None,
        failure_message: str = "A Griptape Cloud request left by a cancelled run failed: %s",
    ) -> None:
        def on_done(finished: asyncio.Future[Any]) -> None:
            cls._background_tasks.discard(finished)
            if finished.cancelled():
                # Only the event loop shutting down cancels this, and nothing can run after that.
                return
            if finished.exception() is not None:
                logger.warning(failure_message, finished.exception())
            if then is not None:
                cls._finish_in_background(
                    asyncio.ensure_future(asyncio.to_thread(then)),
                    failure_message="Failed to delete an upload left by a cancelled run, so it stays in the bucket: %s",
                )

        cls._background_tasks.add(task)
        task.add_done_callback(on_done)

    @staticmethod
    def _is_not_found(error: BaseException) -> bool:
        # The driver wraps HTTP errors in RuntimeError, so look down the cause chain.
        cause: BaseException | None = error
        while cause is not None:
            if isinstance(cause, httpx2.HTTPStatusError) and cause.response.status_code == HTTPStatus.NOT_FOUND:
                return True
            cause = cause.__cause__
        return False

    @staticmethod
    def _is_public(url: str) -> bool:
        return url.startswith(("http://", "https://")) and "localhost" not in url

    @staticmethod
    def _derive_upload_filename(url: str) -> str:
        """Pick the storage filename for a value being uploaded.

        A data URI has no path component to take a name from -- deriving one from the URI
        would embed the (potentially megabytes-long) base64 payload in the storage key --
        so it gets a generated name instead. The extension matters beyond aesthetics: the
        presigned download URL's content type is guessed from the uploaded path's
        extension, so a bare name would serve the media without a content type.
        """
        if url.startswith("data:"):
            mime_type = url.removeprefix("data:").split(";", 1)[0].split(",", 1)[0]
            extension = mimetypes.guess_extension(mime_type) or ""
            return f"{uuid4().hex}{extension}"
        return Path(urlparse(url).path).name or uuid4().hex
