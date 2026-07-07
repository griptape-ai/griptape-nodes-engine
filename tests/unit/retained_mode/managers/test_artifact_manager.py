import json
import os
import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING, cast

import anyio
import pytest
from PIL import Image
from pydantic import ValidationError

from griptape_nodes.common.macro_parser import ParsedMacro
from griptape_nodes.retained_mode.events.artifact_events import (
    GeneratePreviewRequest,
    GeneratePreviewResultFailure,
    GeneratePreviewResultSuccess,
    GetArtifactProviderDetailsRequest,
    GetArtifactProviderDetailsResultFailure,
    GetArtifactProviderDetailsResultSuccess,
    GetPreviewForArtifactRequest,
    GetPreviewForArtifactResultFailure,
    GetPreviewForArtifactResultSuccess,
    ListArtifactProvidersRequest,
    ListArtifactProvidersResultSuccess,
    PreviewGenerationPolicy,
    RegisterArtifactProviderRequest,
    RegisterArtifactProviderResultFailure,
    RegisterArtifactProviderResultSuccess,
)
from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
from griptape_nodes.retained_mode.events.config_events import SetConfigValueResultSuccess
from griptape_nodes.retained_mode.events.project_events import MacroPath
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.artifact_manager import ArtifactManager, PreviewMetadata
from griptape_nodes.retained_mode.managers.artifact_providers import (
    BaseArtifactProvider,
    ImageArtifactProvider,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators.pil_thumbnail_generator import (
        PILThumbnailParameters,
    )

# ==================================================================================
# CRITICAL WARNING: Config Isolation for Tests
# ==================================================================================
# Tests in this file register artifact providers, which triggers config writes
# to the REAL user config files at ~/.config/griptape_nodes/
#
# To prevent test pollution of real config files, we MUST mock
# GriptapeNodes.handle_request to intercept SetConfigValueRequest calls.
#
# Without this mock, running tests will write test provider configs to your
# actual config files, causing the errors you saw in the editor.
# ==================================================================================


@pytest.fixture(autouse=True)
def mock_config_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock GriptapeNodes.handle_request to prevent tests from writing to real config files.

    This fixture is autouse=True, so it applies to ALL tests in this file automatically.
    Any test that registers providers would otherwise pollute the real user config.
    """
    from griptape_nodes.retained_mode.events.config_events import SetConfigValueRequest
    from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

    # Store the original handle_request method
    original_handle_request = GriptapeNodes.handle_request

    def selective_mock(request: RequestPayload) -> ResultPayload:
        """Only mock SetConfigValueRequest, let all other requests through."""
        if isinstance(request, SetConfigValueRequest):
            # Mock config writes to prevent test pollution
            return SetConfigValueResultSuccess(result_details="Mocked config write")
        # Let all other requests go to the real handler
        return original_handle_request(request)

    monkeypatch.setattr(
        "griptape_nodes.retained_mode.managers.artifact_manager.GriptapeNodes.handle_request", selective_mock
    )


class TestArtifactManager:
    """Test ArtifactManager functionality."""

    @pytest.mark.asyncio
    async def test_init_creates_empty_providers(self) -> None:
        """Test that initialization creates empty provider collections and registers defaults."""
        from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete

        manager = ArtifactManager()

        # Initially empty
        assert isinstance(manager._registry._provider_classes, list)
        assert len(manager._registry._provider_classes) == 0

        # Trigger app initialization to register default providers
        await manager.on_app_initialization_complete(AppInitializationComplete())

        # Now default providers should be registered (Image, Video, Audio)
        assert len(manager._registry._provider_classes) == 3  # noqa: PLR2004
        assert isinstance(manager._registry._file_format_to_provider_class, dict)
        assert len(manager._registry._file_format_to_provider_class) > 0
        assert isinstance(manager._registry._provider_instances, dict)
        # Provider is registered but NOT instantiated (lazy instantiation)
        assert len(manager._registry._provider_instances) == 0

    def test_register_new_provider_success(self) -> None:
        """Test successful registration of a new provider."""

        class TestProvider(BaseArtifactProvider):
            @classmethod
            def get_friendly_name(cls) -> str:
                return "Test"

            @classmethod
            def get_supported_formats(cls) -> set[str]:
                return {"test", "tst"}

            @classmethod
            def get_preview_formats(cls) -> set[str]:
                return {"webp"}

            @classmethod
            def get_default_preview_generator(cls) -> str:
                return "Default"

            @classmethod
            def get_default_preview_format(cls) -> str:
                return "webp"

            @classmethod
            def get_default_preview_generators(cls) -> list:
                return []

        manager = ArtifactManager()
        initial_count = len(manager._registry._provider_classes)

        request = RegisterArtifactProviderRequest(provider_class=TestProvider)
        result = manager.on_handle_register_artifact_provider_request(request)

        assert isinstance(result, RegisterArtifactProviderResultSuccess)
        assert len(manager._registry._provider_classes) == initial_count + 1
        assert "test" in manager._registry._file_format_to_provider_class
        assert "tst" in manager._registry._file_format_to_provider_class

    def test_register_provider_adds_to_providers_list(self) -> None:
        """Test that registered provider class is added to _provider_classes list."""
        manager = ArtifactManager()

        # ImageArtifactProvider is not pre-registered anymore
        initial_count = len(manager._registry._provider_classes)

        request = RegisterArtifactProviderRequest(provider_class=ImageArtifactProvider)
        result = manager.on_handle_register_artifact_provider_request(request)

        assert isinstance(result, RegisterArtifactProviderResultSuccess)
        assert len(manager._registry._provider_classes) == initial_count + 1

    def test_register_provider_maps_all_supported_formats(self) -> None:
        """Test that all supported formats are mapped to provider class."""
        manager = ArtifactManager()

        # Register ImageArtifactProvider first
        request = RegisterArtifactProviderRequest(provider_class=ImageArtifactProvider)
        result = manager.on_handle_register_artifact_provider_request(request)
        assert isinstance(result, RegisterArtifactProviderResultSuccess)

        # Check all supported formats are mapped
        image_formats = {"png", "jpg", "jpeg", "gif", "bmp", "webp", "tiff", "tif", "tga"}
        for file_format in image_formats:
            assert file_format in manager._registry._file_format_to_provider_class
            assert len(manager._registry._file_format_to_provider_class[file_format]) == 1
            assert manager._registry._file_format_to_provider_class[file_format][0] is ImageArtifactProvider

    @pytest.mark.asyncio
    async def test_initialization_registers_default_providers(self) -> None:
        """Test that ArtifactManager initialization registers default providers."""
        from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete

        manager = ArtifactManager()

        # Initially empty
        assert len(manager._registry._provider_classes) == 0

        # Trigger app initialization to register default providers
        await manager.on_app_initialization_complete(AppInitializationComplete())

        # Now providers should be registered (Image, Video, Audio)
        assert len(manager._registry._provider_classes) == 3  # noqa: PLR2004
        assert "jpg" in manager._registry._file_format_to_provider_class

    def test_multiple_providers_can_handle_same_format(self) -> None:
        """Test that multiple provider classes can be registered for the same format."""

        class AlternateImageProvider(BaseArtifactProvider):
            @classmethod
            def get_friendly_name(cls) -> str:
                return "AlternateImage"

            @classmethod
            def get_supported_formats(cls) -> set[str]:
                return {"jpg", "png"}

            @classmethod
            def get_preview_formats(cls) -> set[str]:
                return {"webp"}

            @classmethod
            def get_default_preview_generator(cls) -> str:
                return "Default"

            @classmethod
            def get_default_preview_format(cls) -> str:
                return "webp"

            @classmethod
            def get_default_preview_generators(cls) -> list:
                return []

        manager = ArtifactManager()

        # Register ImageArtifactProvider first (no longer auto-registered)
        manager.on_handle_register_artifact_provider_request(
            RegisterArtifactProviderRequest(provider_class=ImageArtifactProvider)
        )

        # Register AlternateImageProvider
        request = RegisterArtifactProviderRequest(provider_class=AlternateImageProvider)
        manager.on_handle_register_artifact_provider_request(request)

        expected_provider_count = 2
        assert len(manager._registry._provider_classes) == expected_provider_count
        assert len(manager._registry._file_format_to_provider_class["jpg"]) == expected_provider_count
        assert len(manager._registry._file_format_to_provider_class["png"]) == expected_provider_count

    def test_duplicate_friendly_name_fails_registration(self) -> None:
        """Test that registering a provider class with duplicate friendly name fails."""

        class DuplicateImageProvider(BaseArtifactProvider):
            @classmethod
            def get_friendly_name(cls) -> str:
                return "Image"

            @classmethod
            def get_supported_formats(cls) -> set[str]:
                return {"bmp"}

            @classmethod
            def get_preview_formats(cls) -> set[str]:
                return {"webp"}

            @classmethod
            def get_default_preview_generator(cls) -> str:
                return "Default"

            @classmethod
            def get_default_preview_format(cls) -> str:
                return "webp"

            @classmethod
            def get_default_preview_generators(cls) -> list:
                return []

        manager = ArtifactManager()

        # Register ImageArtifactProvider first (no longer auto-registered)
        manager.on_handle_register_artifact_provider_request(
            RegisterArtifactProviderRequest(provider_class=ImageArtifactProvider)
        )

        # Try to register DuplicateImageProvider with same friendly name
        request = RegisterArtifactProviderRequest(provider_class=DuplicateImageProvider)
        result = manager.on_handle_register_artifact_provider_request(request)

        assert isinstance(result, RegisterArtifactProviderResultFailure)
        assert "duplicate friendly name" in str(result.result_details)
        assert "Image" in str(result.result_details)

    def test_duplicate_friendly_name_case_insensitive(self) -> None:
        """Test that friendly name duplicate detection is case-insensitive."""

        class LowercaseImageProvider(BaseArtifactProvider):
            @classmethod
            def get_friendly_name(cls) -> str:
                return "image"

            @classmethod
            def get_supported_formats(cls) -> set[str]:
                return {"bmp"}

            @classmethod
            def get_preview_formats(cls) -> set[str]:
                return {"webp"}

            @classmethod
            def get_default_preview_generator(cls) -> str:
                return "Default"

            @classmethod
            def get_default_preview_format(cls) -> str:
                return "webp"

            @classmethod
            def get_default_preview_generators(cls) -> list:
                return []

        manager = ArtifactManager()

        # Register ImageArtifactProvider first (no longer auto-registered)
        manager.on_handle_register_artifact_provider_request(
            RegisterArtifactProviderRequest(provider_class=ImageArtifactProvider)
        )
        request = RegisterArtifactProviderRequest(provider_class=LowercaseImageProvider)

        result = manager.on_handle_register_artifact_provider_request(request)

        assert isinstance(result, RegisterArtifactProviderResultFailure)
        assert "duplicate friendly name" in str(result.result_details)

    def test_get_provider_class_by_friendly_name_case_insensitive(self) -> None:
        """Test that registry lookup is case-insensitive."""
        manager = ArtifactManager()

        # Register ImageArtifactProvider (no longer auto-registered)
        manager.on_handle_register_artifact_provider_request(
            RegisterArtifactProviderRequest(provider_class=ImageArtifactProvider)
        )

        provider_class_lower = manager._registry.get_provider_class_by_friendly_name("image")
        provider_class_title = manager._registry.get_provider_class_by_friendly_name("Image")
        provider_class_upper = manager._registry.get_provider_class_by_friendly_name("IMAGE")
        provider_class_missing = manager._registry.get_provider_class_by_friendly_name("Video")

        assert provider_class_lower is not None
        assert provider_class_title is not None
        assert provider_class_upper is not None
        assert provider_class_lower is provider_class_title
        assert provider_class_title is provider_class_upper
        assert provider_class_lower is ImageArtifactProvider
        assert provider_class_missing is None

    def test_lazy_instantiation_creates_singleton(self) -> None:
        """Test that registry lazy instantiation creates and caches singleton."""
        manager = ArtifactManager()

        assert len(manager._registry._provider_instances) == 0

        instance1 = manager._registry.get_or_create_provider_instance(ImageArtifactProvider)
        assert isinstance(instance1, ImageArtifactProvider)
        assert len(manager._registry._provider_instances) == 1

        instance2 = manager._registry.get_or_create_provider_instance(ImageArtifactProvider)
        assert instance2 is instance1
        assert len(manager._registry._provider_instances) == 1

    def test_list_artifact_providers_returns_friendly_names(self) -> None:
        """Test that ListArtifactProvidersRequest returns list of friendly names."""
        manager = ArtifactManager()

        # Register ImageArtifactProvider (no longer auto-registered)
        manager.on_handle_register_artifact_provider_request(
            RegisterArtifactProviderRequest(provider_class=ImageArtifactProvider)
        )
        # ImageArtifactProvider is already registered in constructor

        list_request = ListArtifactProvidersRequest()
        result = manager.on_handle_list_artifact_providers_request(list_request)

        assert isinstance(result, ListArtifactProvidersResultSuccess)
        assert len(result.friendly_names) == 1
        assert "Image" in result.friendly_names

    def test_get_artifact_provider_details_success(self) -> None:
        """Test that GetArtifactProviderDetailsRequest returns provider details."""
        manager = ArtifactManager()

        # Register ImageArtifactProvider (no longer auto-registered)
        manager.on_handle_register_artifact_provider_request(
            RegisterArtifactProviderRequest(provider_class=ImageArtifactProvider)
        )

        details_request = GetArtifactProviderDetailsRequest(friendly_name="image")
        result = manager.on_handle_get_artifact_provider_details_request(details_request)

        assert isinstance(result, GetArtifactProviderDetailsResultSuccess)
        assert result.friendly_name == "Image"
        assert "jpg" in result.supported_formats
        assert "png" in result.supported_formats
        assert "webp" in result.preview_formats

    def test_get_artifact_provider_details_not_found(self) -> None:
        """Test that GetArtifactProviderDetailsRequest fails when provider not found."""
        manager = ArtifactManager()

        details_request = GetArtifactProviderDetailsRequest(friendly_name="Video")
        result = manager.on_handle_get_artifact_provider_details_request(details_request)

        assert isinstance(result, GetArtifactProviderDetailsResultFailure)
        assert "provider not found" in str(result.result_details)
        assert "Video" in str(result.result_details)


class TestPermissionDispatch:
    """ArtifactManager routes write/read permission checks through the matching provider.

    A provider is looked up by format (bytes for writes, extension for reads);
    the provider's hook decides. Unknown format / unknown extension falls
    through to allow so callers don't need to special-case unregistered types.
    """

    _PROBE_FORMAT = "probe"

    def _make_probe_provider_class(  # noqa: ANN202
        self,
        denial_for_write_from_bytes=None,  # noqa: ANN001
        denial_for_write_from_path=None,  # noqa: ANN001
        denial_for_read=None,  # noqa: ANN001
        write_vetting_policy=None,  # noqa: ANN001
    ):
        from griptape_nodes.retained_mode.managers.artifact_providers.base_artifact_provider import (
            BaseArtifactMetadata,
            BaseArtifactProvider,
        )

        probe_format = self._PROBE_FORMAT
        declared_policy = write_vetting_policy

        class _ProbeProvider(BaseArtifactProvider):
            calls: list[tuple[str, object]] = []  # noqa: RUF012

            @classmethod
            def get_friendly_name(cls) -> str:
                return "Probe"

            @classmethod
            def get_supported_formats(cls) -> set[str]:
                return {probe_format}

            @classmethod
            def get_artifact_metadata(cls, source_path: str) -> BaseArtifactMetadata | None:  # noqa: ARG003
                return None

            @staticmethod
            def get_write_vetting_policy():  # noqa: ANN205
                return declared_policy

            def check_write_format_from_bytes(self, data, detected_format):  # noqa: ANN001, ANN202, ARG002
                self.__class__.calls.append(("write_from_bytes", detected_format))
                return denial_for_write_from_bytes

            def check_write_format_from_path(self, source_path, detected_format):  # noqa: ANN001, ANN202, ARG002
                self.__class__.calls.append(("write_from_path", detected_format))
                return denial_for_write_from_path

            def check_read_permission(self, source_path):  # noqa: ANN001, ANN202
                self.__class__.calls.append(("read", source_path))
                return denial_for_read

        return _ProbeProvider

    def _register(self, manager: ArtifactManager, provider_class: type) -> None:
        result = manager.on_handle_register_artifact_provider_request(
            RegisterArtifactProviderRequest(provider_class=provider_class)
        )
        assert isinstance(result, RegisterArtifactProviderResultSuccess)

    def test_get_write_vetting_policy_reflects_provider(self) -> None:
        from griptape_nodes.retained_mode.managers.artifact_providers.base_artifact_provider import WriteVettingPolicy

        probe_cls = self._make_probe_provider_class(write_vetting_policy=WriteVettingPolicy.FROM_PATH)
        manager = ArtifactManager()
        self._register(manager, probe_cls)

        assert manager.get_write_vetting_policy(self._PROBE_FORMAT) is WriteVettingPolicy.FROM_PATH

    def test_get_write_vetting_policy_none_when_no_provider(self) -> None:
        manager = ArtifactManager()
        assert manager.get_write_vetting_policy("unregistered") is None

    def test_check_write_format_from_bytes_dispatches_to_provider_by_format(self) -> None:
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure

        expected_denial = CheckpointDenial(failures=(CheckpointFailure(detail="probe format is disallowed"),))
        probe_cls = self._make_probe_provider_class(denial_for_write_from_bytes=expected_denial)
        manager = ArtifactManager()
        self._register(manager, probe_cls)

        denial = manager.check_write_format_from_bytes(b"any bytes", self._PROBE_FORMAT)

        assert denial is expected_denial
        assert probe_cls.calls == [("write_from_bytes", self._PROBE_FORMAT)]

    def test_check_write_format_from_path_dispatches_to_provider_by_format(self) -> None:
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure

        expected_denial = CheckpointDenial(failures=(CheckpointFailure(detail="probe format is disallowed"),))
        probe_cls = self._make_probe_provider_class(denial_for_write_from_path=expected_denial)
        manager = ArtifactManager()
        self._register(manager, probe_cls)

        denial = manager.check_write_format_from_path("/tmp/staged.probe", self._PROBE_FORMAT)  # noqa: S108

        assert denial is expected_denial
        assert probe_cls.calls == [("write_from_path", self._PROBE_FORMAT)]

    def test_check_write_format_from_bytes_no_provider_falls_through(self) -> None:
        # An unregistered format must not raise or reach any provider -- it's
        # not the ArtifactManager's job to know every possible extension.
        manager = ArtifactManager()

        assert manager.check_write_format_from_bytes(b"data", "unregistered") is None

    def test_check_write_format_from_path_no_provider_falls_through(self) -> None:
        manager = ArtifactManager()

        assert manager.check_write_format_from_path("/tmp/nothing.dat", "unregistered") is None  # noqa: S108

    def test_check_read_permission_dispatches_by_extension(self) -> None:
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure

        expected_denial = CheckpointDenial(failures=(CheckpointFailure(detail="probe reads are locked"),))
        probe_cls = self._make_probe_provider_class(denial_for_read=expected_denial)
        manager = ArtifactManager()
        self._register(manager, probe_cls)

        denial = manager.check_read_permission(f"/some/file.{self._PROBE_FORMAT}")

        assert denial is expected_denial
        # The provider is called with the same path the manager was given,
        # verbatim -- provider does its own inspection (e.g. ffprobe).
        assert probe_cls.calls == [("read", f"/some/file.{self._PROBE_FORMAT}")]

    def test_check_read_permission_unknown_extension_falls_through(self) -> None:
        manager = ArtifactManager()
        assert manager.check_read_permission("/some/file.unregistered") is None

    def test_check_read_permission_no_extension_falls_through(self) -> None:
        # A path with no extension cannot be routed to any provider; allow
        # rather than raising so path-less inputs don't crash the check.
        manager = ArtifactManager()
        assert manager.check_read_permission("/some/file_without_ext") is None


class TestCheckArtifactReadPermissionHandler:
    """The request-based read-permission check.

    Library code sends this instead of importing a provider directly, so it
    stays media-agnostic. The handler wraps ``check_read_permission`` and
    returns the denial (if any) on the Success payload.
    """

    def test_empty_path_returns_failure(self) -> None:
        from griptape_nodes.retained_mode.events.artifact_events import (
            CheckArtifactReadPermissionRequest,
            CheckArtifactReadPermissionResultFailure,
        )

        manager = ArtifactManager()
        result = manager.on_check_artifact_read_permission_request(CheckArtifactReadPermissionRequest(source_path=""))

        assert isinstance(result, CheckArtifactReadPermissionResultFailure)
        assert "no source path" in str(result.result_details)

    def test_no_provider_returns_success_with_no_denial(self) -> None:
        # Unregistered extension: allow. The handler must NOT return Failure --
        # the request itself is well-formed; the answer is just "not gated".
        from griptape_nodes.retained_mode.events.artifact_events import (
            CheckArtifactReadPermissionRequest,
            CheckArtifactReadPermissionResultSuccess,
        )

        manager = ArtifactManager()
        result = manager.on_check_artifact_read_permission_request(
            CheckArtifactReadPermissionRequest(source_path="/x/y.unregistered")
        )

        assert isinstance(result, CheckArtifactReadPermissionResultSuccess)
        assert result.denial is None

    def test_denial_is_threaded_through_success_payload(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.retained_mode.events.artifact_events import (
            CheckArtifactReadPermissionRequest,
            CheckArtifactReadPermissionResultSuccess,
        )
        from griptape_nodes.retained_mode.managers.artifact_providers.base_artifact_provider import (
            BaseArtifactMetadata,
            BaseArtifactProvider,
        )
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
            AuthorizationCheckpoint,
            CheckpointDenial,
            CheckpointFailure,
        )

        expected_denial = CheckpointDenial(
            failures=(CheckpointFailure(detail="You are not licensed for probe reads."),)
        )

        class _DenyProvider(BaseArtifactProvider):
            @classmethod
            def get_friendly_name(cls) -> str:
                return "Deny"

            @classmethod
            def get_supported_formats(cls) -> set[str]:
                return {"deny"}

            @classmethod
            def get_artifact_metadata(cls, source_path: str) -> BaseArtifactMetadata | None:  # noqa: ARG003
                return None

            def check_read_permission(self, source_path):  # noqa: ANN001, ANN202, ARG002
                return expected_denial

        manager = ArtifactManager()
        register_result = manager.on_handle_register_artifact_provider_request(
            RegisterArtifactProviderRequest(provider_class=_DenyProvider)
        )
        assert isinstance(register_result, RegisterArtifactProviderResultSuccess)

        # Register a sentinel hook so the short-circuit doesn't skip the vet.
        # The hook itself isn't consulted here (the provider returns its own
        # denial before the checkpoint would fire) -- it just satisfies
        # ``has_authorization_hooks()``.
        def sentinel_hook(_checkpoint: AuthorizationCheckpoint) -> CheckpointDenial | None:
            return None

        griptape_nodes.EventManager().add_authorization_hook(sentinel_hook)
        try:
            result = manager.on_check_artifact_read_permission_request(
                CheckArtifactReadPermissionRequest(source_path="/x/y.deny")
            )
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(sentinel_hook)

        assert isinstance(result, CheckArtifactReadPermissionResultSuccess)
        assert result.denial is expected_denial
        # The result_details includes the denial's reason so callers logging
        # the result get useful text without inspecting the payload.
        assert "probe reads" in str(result.result_details)

    def test_vet_skipped_when_no_authorization_hook_registered(self, griptape_nodes: GriptapeNodes) -> None:
        """Perf short-circuit: with no auth hook registered, the provider's check_read_permission is not called.

        The read-side gate has the same short-circuit as the write-side vet.
        Without a hook the provider's read check (ffprobe subprocess) is
        skipped -- outcome is guaranteed to be None anyway.
        """
        from griptape_nodes.retained_mode.events.artifact_events import (
            CheckArtifactReadPermissionRequest,
            CheckArtifactReadPermissionResultSuccess,
        )
        from griptape_nodes.retained_mode.managers.artifact_providers.base_artifact_provider import (
            BaseArtifactMetadata,
            BaseArtifactProvider,
        )

        called: list[bool] = []

        class _SpyProvider(BaseArtifactProvider):
            @classmethod
            def get_friendly_name(cls) -> str:
                return "Spy"

            @classmethod
            def get_supported_formats(cls) -> set[str]:
                return {"spy"}

            @classmethod
            def get_artifact_metadata(cls, source_path: str) -> BaseArtifactMetadata | None:  # noqa: ARG003
                return None

            def check_read_permission(self, source_path):  # noqa: ANN001, ANN202, ARG002
                called.append(True)

        manager = ArtifactManager()
        manager.on_handle_register_artifact_provider_request(
            RegisterArtifactProviderRequest(provider_class=_SpyProvider)
        )

        # Baseline: no hook registered.
        assert griptape_nodes.EventManager().has_authorization_hooks() is False

        result = manager.on_check_artifact_read_permission_request(
            CheckArtifactReadPermissionRequest(source_path="/x/y.spy")
        )

        assert isinstance(result, CheckArtifactReadPermissionResultSuccess)
        assert result.denial is None
        assert called == [], "check_read_permission must not be called when no hook is registered"


class TestGeneratePreview:
    """Tests for preview generation functionality."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create temporary directory for test files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def mock_project(self, temp_dir: Path) -> None:
        """Set up a real project in ProjectManager with temp_dir as workspace."""
        from griptape_nodes.common.project_templates import ProjectValidationInfo, ProjectValidationStatus
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
        from griptape_nodes.retained_mode.managers.project_manager import ProjectInfo

        # Get ProjectManager singleton
        project_manager = GriptapeNodes.ProjectManager()

        # Parse macros for the template
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        situation_schemas = project_manager._parse_situation_macros(DEFAULT_PROJECT_TEMPLATE.situations, validation)
        directory_schemas = project_manager._parse_directory_macros(DEFAULT_PROJECT_TEMPLATE.directories, validation)

        # Create ProjectInfo with temp_dir as workspace
        project_info = ProjectInfo(
            project_id="test_project",
            project_file_path=temp_dir / "project.yml",
            project_base_dir=temp_dir,
            template=DEFAULT_PROJECT_TEMPLATE,
            validation=validation,
            parsed_situation_schemas=situation_schemas,
            parsed_directory_schemas=directory_schemas,
        )

        # Load the project into ProjectManager
        project_manager._successfully_loaded_project_templates["test_project"] = project_info
        project_manager._current_project_id = "test_project"

    @pytest.fixture
    def test_image_path(self, temp_dir: Path) -> Path:
        """Create a real test image file for preview generation.

        Returns:
            Path to a 100x100 JPEG test image with known properties
        """
        image_path = temp_dir / "test_source.jpg"

        # Create a simple test image (100x100 red square)
        img = Image.new("RGB", (100, 100), color="red")
        img.save(str(image_path), format="JPEG")

        return image_path

    @pytest.fixture
    def test_macro_path(self, test_image_path: Path) -> MacroPath:
        """Create MacroPath for test image."""
        parsed_macro = ParsedMacro(str(test_image_path))
        return MacroPath(parsed_macro=parsed_macro, variables={})

    @pytest.fixture
    def artifact_manager(self, mock_project: None, temp_dir: Path) -> ArtifactManager:  # noqa: ARG002
        """Create ArtifactManager instance with ImageArtifactProvider registered."""
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        manager = ArtifactManager()
        # Register ImageArtifactProvider (no longer auto-registered)
        request = RegisterArtifactProviderRequest(provider_class=ImageArtifactProvider)
        manager.on_handle_register_artifact_provider_request(request)
        # Set workspace_path after provider registration since registration triggers load_configs()
        GriptapeNodes.ConfigManager().workspace_path = temp_dir
        return manager

    @pytest.mark.asyncio
    async def test_generate_preview_without_metadata_success(
        self, artifact_manager: ArtifactManager, test_macro_path: MacroPath, test_image_path: Path
    ) -> None:
        """Test generating preview without metadata."""
        request = GeneratePreviewRequest(
            macro_path=test_macro_path,
            artifact_provider_name="Image",
            format=None,
            generate_preview_metadata_json=False,
            preview_generator_parameters={"max_width": 50, "max_height": 50},
        )

        result = await artifact_manager.on_handle_generate_preview_request(request)

        assert isinstance(result, GeneratePreviewResultSuccess)

        # Verify preview file exists
        preview_dir = test_image_path.parent / ".griptape-nodes-previews"
        preview_path = preview_dir / f"{test_image_path.name}.webp"
        assert preview_path.exists()

        # Verify preview dimensions
        with Image.open(str(preview_path)) as preview_img:
            assert preview_img.width <= 50  # noqa: PLR2004
            assert preview_img.height <= 50  # noqa: PLR2004

        # Verify no metadata file
        metadata_path = anyio.Path(str(preview_path) + ".json")
        assert not await metadata_path.exists()

    @pytest.mark.asyncio
    async def test_generate_preview_with_metadata_success(
        self, artifact_manager: ArtifactManager, test_macro_path: MacroPath, test_image_path: Path
    ) -> None:
        """Test generating preview with metadata."""
        request = GeneratePreviewRequest(
            macro_path=test_macro_path,
            artifact_provider_name="Image",
            format=None,
            generate_preview_metadata_json=True,
            preview_generator_parameters={"max_width": 50, "max_height": 50},
        )

        result = await artifact_manager.on_handle_generate_preview_request(request)

        assert isinstance(result, GeneratePreviewResultSuccess)

        # Verify preview file exists
        preview_dir = test_image_path.parent / ".griptape-nodes-previews"
        preview_path = preview_dir / f"{test_image_path.name}.webp"
        assert preview_path.exists()

        # Verify metadata file exists (named after source file, not preview)
        metadata_path = preview_dir / f"{test_image_path.name}.json"
        assert metadata_path.exists()

        # Verify metadata contents
        with metadata_path.open() as f:
            metadata_dict = json.load(f)

        assert "version" in metadata_dict
        assert "source_macro_path" in metadata_dict
        assert "source_file_size" in metadata_dict
        assert "source_file_modified_time" in metadata_dict
        assert "preview_file_names" in metadata_dict
        assert "preview_generator_name" in metadata_dict
        assert "preview_generator_parameters" in metadata_dict

        # Verify metadata values match source file
        source_stat = await anyio.Path(test_image_path).stat()
        assert metadata_dict["source_file_size"] == source_stat.st_size
        assert metadata_dict["source_file_modified_time"] == source_stat.st_mtime
        assert metadata_dict["preview_file_names"] == f"{test_image_path.name}.webp"
        assert metadata_dict["preview_generator_name"] == "Standard Thumbnail Generation"
        assert metadata_dict["preview_generator_parameters"] == {"max_width": 50, "max_height": 50}
        assert metadata_dict["version"] == PreviewMetadata.LATEST_SCHEMA_VERSION

    @pytest.mark.asyncio
    async def test_generate_preview_source_file_not_found(
        self, artifact_manager: ArtifactManager, temp_dir: Path
    ) -> None:
        """Test generating preview for non-existent file."""
        nonexistent_path = temp_dir / "nonexistent.jpg"
        parsed_macro = ParsedMacro(str(nonexistent_path))
        macro_path = MacroPath(parsed_macro=parsed_macro, variables={})

        request = GeneratePreviewRequest(
            macro_path=macro_path,
            artifact_provider_name="Image",
            format=None,
            generate_preview_metadata_json=False,
            preview_generator_parameters={"max_width": 50, "max_height": 50},
        )

        result = await artifact_manager.on_handle_generate_preview_request(request)

        assert isinstance(result, GeneratePreviewResultFailure)
        assert "file not found" in str(result.result_details).lower()

    @pytest.mark.asyncio
    async def test_generate_preview_unsupported_format(self, artifact_manager: ArtifactManager, temp_dir: Path) -> None:
        """Test generating preview for unsupported format."""
        # Create a .txt file
        txt_path = temp_dir / "test.txt"
        txt_path.write_text("This is not an image")

        parsed_macro = ParsedMacro(str(txt_path))
        macro_path = MacroPath(parsed_macro=parsed_macro, variables={})

        request = GeneratePreviewRequest(
            macro_path=macro_path,
            artifact_provider_name="Image",
            format=None,
            generate_preview_metadata_json=False,
            preview_generator_parameters={"max_width": 50, "max_height": 50},
        )

        result = await artifact_manager.on_handle_generate_preview_request(request)

        assert isinstance(result, GeneratePreviewResultFailure)
        assert "provider" in str(result.result_details).lower() or "format" in str(result.result_details).lower()

    @pytest.mark.asyncio
    async def test_generate_preview_custom_dimensions(
        self, artifact_manager: ArtifactManager, test_macro_path: MacroPath, test_image_path: Path
    ) -> None:
        """Test generating preview with custom dimensions."""
        request = GeneratePreviewRequest(
            macro_path=test_macro_path,
            artifact_provider_name="Image",
            format=None,
            generate_preview_metadata_json=False,
            preview_generator_parameters={"max_width": 30, "max_height": 40},
        )

        result = await artifact_manager.on_handle_generate_preview_request(request)

        assert isinstance(result, GeneratePreviewResultSuccess)

        # Verify preview dimensions respect constraints
        preview_dir = test_image_path.parent / ".griptape-nodes-previews"
        preview_path = preview_dir / f"{test_image_path.name}.webp"

        with Image.open(str(preview_path)) as preview_img:
            assert preview_img.width <= 30  # noqa: PLR2004
            assert preview_img.height <= 40  # noqa: PLR2004
            # Verify aspect ratio preserved (source is 100x100, so should be square)
            assert preview_img.width == preview_img.height

    @pytest.mark.asyncio
    async def test_generate_preview_specific_format(
        self, artifact_manager: ArtifactManager, test_macro_path: MacroPath, test_image_path: Path
    ) -> None:
        """Test generating preview with specific format."""
        request = GeneratePreviewRequest(
            macro_path=test_macro_path,
            artifact_provider_name="Image",
            format="webp",
            generate_preview_metadata_json=True,
            preview_generator_parameters={"max_width": 50, "max_height": 50},
        )

        result = await artifact_manager.on_handle_generate_preview_request(request)

        assert isinstance(result, GeneratePreviewResultSuccess)

        # Verify preview file has correct extension
        preview_dir = test_image_path.parent / ".griptape-nodes-previews"
        preview_path = preview_dir / f"{test_image_path.name}.webp"
        assert preview_path.exists()

        # Verify metadata has correct extension (named after source file)
        metadata_path = preview_dir / f"{test_image_path.name}.json"
        with metadata_path.open() as f:
            metadata_dict = json.load(f)
        assert metadata_dict["preview_file_names"] == f"{test_image_path.name}.webp"

    @pytest.mark.asyncio
    async def test_generate_preview_specific_generator(
        self, artifact_manager: ArtifactManager, test_macro_path: MacroPath, test_image_path: Path
    ) -> None:
        """Test generating preview with specific generator."""
        request = GeneratePreviewRequest(
            macro_path=test_macro_path,
            artifact_provider_name="Image",
            format=None,
            preview_generator_name="Standard Thumbnail Generation",
            generate_preview_metadata_json=False,
            preview_generator_parameters={"max_width": 50, "max_height": 50},
        )

        result = await artifact_manager.on_handle_generate_preview_request(request)

        assert isinstance(result, GeneratePreviewResultSuccess)

        # Verify preview was created
        preview_dir = test_image_path.parent / ".griptape-nodes-previews"
        preview_path = preview_dir / f"{test_image_path.name}.webp"
        assert preview_path.exists()

    @pytest.mark.asyncio
    async def test_generate_preview_metadata_serialization_preserves_structure(
        self, artifact_manager: ArtifactManager, test_macro_path: MacroPath, test_image_path: Path
    ) -> None:
        """Test that metadata can be deserialized back to PreviewMetadata."""
        request = GeneratePreviewRequest(
            macro_path=test_macro_path,
            artifact_provider_name="Image",
            format=None,
            generate_preview_metadata_json=True,
            preview_generator_parameters={"max_width": 50, "max_height": 50},
        )

        result = await artifact_manager.on_handle_generate_preview_request(request)

        assert isinstance(result, GeneratePreviewResultSuccess)

        # Read metadata and deserialize with Pydantic
        preview_dir = test_image_path.parent / ".griptape-nodes-previews"
        # Metadata is named after source file, not preview
        metadata_path = preview_dir / f"{test_image_path.name}.json"

        with metadata_path.open() as f:
            metadata_dict = json.load(f)

        # Verify can deserialize to PreviewMetadata using Pydantic
        metadata = PreviewMetadata.model_validate(metadata_dict)

        assert metadata.version == PreviewMetadata.LATEST_SCHEMA_VERSION
        assert metadata.source_macro_path == str(test_image_path)
        assert metadata.source_file_size > 0
        assert metadata.source_file_modified_time > 0
        assert metadata.preview_file_names == f"{test_image_path.name}.webp"
        assert metadata.preview_generator_name == "Standard Thumbnail Generation"
        assert isinstance(metadata.preview_generator_parameters, dict)


class TestPreviewMetadataDoesNotCreateSidecar:
    """Tests that preview metadata JSON files do not trigger sidecar creation.

    When a preview is generated with metadata, the metadata JSON file is written
    to .griptape-nodes-previews/ via WriteFileRequest. That request must NOT
    pass file_metadata, otherwise write_sidecar() creates a redundant sidecar
    in .griptape-nodes-metadata/ with a nested .griptape-nodes-previews/ directory
    and .json.json double extensions.
    """

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create temporary directory for test files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def mock_project(self, temp_dir: Path) -> None:
        """Set up a real project in ProjectManager with temp_dir as workspace."""
        from griptape_nodes.common.project_templates import ProjectValidationInfo, ProjectValidationStatus
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
        from griptape_nodes.retained_mode.managers.project_manager import ProjectInfo

        project_manager = GriptapeNodes.ProjectManager()

        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        situation_schemas = project_manager._parse_situation_macros(DEFAULT_PROJECT_TEMPLATE.situations, validation)
        directory_schemas = project_manager._parse_directory_macros(DEFAULT_PROJECT_TEMPLATE.directories, validation)

        project_info = ProjectInfo(
            project_id="test_project",
            project_file_path=temp_dir / "project.yml",
            project_base_dir=temp_dir,
            template=DEFAULT_PROJECT_TEMPLATE,
            validation=validation,
            parsed_situation_schemas=situation_schemas,
            parsed_directory_schemas=directory_schemas,
        )

        project_manager._successfully_loaded_project_templates["test_project"] = project_info
        project_manager._current_project_id = "test_project"

    @pytest.fixture
    def test_image_path(self, temp_dir: Path) -> Path:
        """Create a real test image file."""
        image_path = temp_dir / "test_source.jpg"
        img = Image.new("RGB", (100, 100), color="red")
        img.save(str(image_path), format="JPEG")
        return image_path

    @pytest.fixture
    def test_macro_path(self, test_image_path: Path) -> MacroPath:
        """Create MacroPath for test image."""
        parsed_macro = ParsedMacro(str(test_image_path))
        return MacroPath(parsed_macro=parsed_macro, variables={})

    @pytest.fixture
    def artifact_manager(self, mock_project: None, temp_dir: Path) -> ArtifactManager:  # noqa: ARG002
        """Create ArtifactManager instance with ImageArtifactProvider registered."""
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        manager = ArtifactManager()
        request = RegisterArtifactProviderRequest(provider_class=ImageArtifactProvider)
        manager.on_handle_register_artifact_provider_request(request)
        GriptapeNodes.ConfigManager().workspace_path = temp_dir
        return manager

    @pytest.mark.asyncio
    async def test_no_sidecar_created_for_preview_metadata_json(
        self, artifact_manager: ArtifactManager, test_macro_path: MacroPath, temp_dir: Path
    ) -> None:
        """Test that generating a preview with metadata does not create a sidecar for the metadata JSON."""
        request = GeneratePreviewRequest(
            macro_path=test_macro_path,
            artifact_provider_name="Image",
            format=None,
            generate_preview_metadata_json=True,
            preview_generator_parameters={"max_width": 50, "max_height": 50},
        )

        result = await artifact_manager.on_handle_generate_preview_request(request)
        assert isinstance(result, GeneratePreviewResultSuccess)

        metadata_dir = anyio.Path(temp_dir / ".griptape-nodes-metadata")
        if await metadata_dir.exists():
            sidecar_files = [f async for f in metadata_dir.rglob("*") if await f.is_file()]
            assert sidecar_files == [], (
                f"Sidecar files were created in .griptape-nodes-metadata/ for preview metadata: {sidecar_files}"
            )

    @pytest.mark.asyncio
    async def test_no_nested_previews_dir_in_metadata(
        self, artifact_manager: ArtifactManager, test_macro_path: MacroPath, temp_dir: Path
    ) -> None:
        """Test that .griptape-nodes-metadata/ does not contain a nested .griptape-nodes-previews/ dir."""
        request = GeneratePreviewRequest(
            macro_path=test_macro_path,
            artifact_provider_name="Image",
            format=None,
            generate_preview_metadata_json=True,
            preview_generator_parameters={"max_width": 50, "max_height": 50},
        )

        result = await artifact_manager.on_handle_generate_preview_request(request)
        assert isinstance(result, GeneratePreviewResultSuccess)

        nested_previews = anyio.Path(temp_dir / ".griptape-nodes-metadata" / ".griptape-nodes-previews")
        assert not await nested_previews.exists(), (
            ".griptape-nodes-previews/ was nested inside .griptape-nodes-metadata/"
        )

    @pytest.mark.asyncio
    async def test_no_double_json_extension_files(
        self, artifact_manager: ArtifactManager, test_macro_path: MacroPath, temp_dir: Path
    ) -> None:
        """Test that no .json.json files are created anywhere in the project."""
        request = GeneratePreviewRequest(
            macro_path=test_macro_path,
            artifact_provider_name="Image",
            format=None,
            generate_preview_metadata_json=True,
            preview_generator_parameters={"max_width": 50, "max_height": 50},
        )

        result = await artifact_manager.on_handle_generate_preview_request(request)
        assert isinstance(result, GeneratePreviewResultSuccess)

        double_json_files = [f async for f in anyio.Path(temp_dir).rglob("*.json.json")]
        assert double_json_files == [], f"Files with .json.json double extension were created: {double_json_files}"


class TestGetPreviewForArtifact:
    """Tests for preview retrieval functionality."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create temporary directory for test files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def mock_project(self, temp_dir: Path) -> None:
        """Set up a real project in ProjectManager with temp_dir as workspace."""
        from griptape_nodes.common.project_templates import ProjectValidationInfo, ProjectValidationStatus
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
        from griptape_nodes.retained_mode.managers.project_manager import ProjectInfo

        # Get ProjectManager singleton
        project_manager = GriptapeNodes.ProjectManager()

        # Parse macros for the template
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        situation_schemas = project_manager._parse_situation_macros(DEFAULT_PROJECT_TEMPLATE.situations, validation)
        directory_schemas = project_manager._parse_directory_macros(DEFAULT_PROJECT_TEMPLATE.directories, validation)

        # Create ProjectInfo with temp_dir as workspace
        project_info = ProjectInfo(
            project_id="test_project",
            project_file_path=temp_dir / "project.yml",
            project_base_dir=temp_dir,
            template=DEFAULT_PROJECT_TEMPLATE,
            validation=validation,
            parsed_situation_schemas=situation_schemas,
            parsed_directory_schemas=directory_schemas,
        )

        # Load the project into ProjectManager
        project_manager._successfully_loaded_project_templates["test_project"] = project_info
        project_manager._current_project_id = "test_project"

    @pytest.fixture
    def test_image_path(self, temp_dir: Path) -> Path:
        """Create a real test image file."""
        image_path = temp_dir / "test_source.jpg"

        # Create a simple test image (100x100 red square)
        img = Image.new("RGB", (100, 100), color="red")
        img.save(str(image_path), format="JPEG")

        return image_path

    @pytest.fixture
    def test_macro_path(self, test_image_path: Path) -> MacroPath:
        """Create MacroPath for test image."""
        parsed_macro = ParsedMacro(str(test_image_path))
        return MacroPath(parsed_macro=parsed_macro, variables={})

    @pytest.fixture
    def artifact_manager(self, mock_project: None, temp_dir: Path) -> ArtifactManager:  # noqa: ARG002
        """Create ArtifactManager with ImageArtifactProvider registered."""
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        manager = ArtifactManager()
        # Register ImageArtifactProvider (no longer auto-registered)
        request = RegisterArtifactProviderRequest(provider_class=ImageArtifactProvider)
        manager.on_handle_register_artifact_provider_request(request)
        # Set workspace_path after provider registration since registration triggers load_configs()
        GriptapeNodes.ConfigManager().workspace_path = temp_dir
        return manager

    @pytest.fixture
    def generated_preview_with_metadata(self, artifact_manager: ArtifactManager, test_macro_path: MacroPath) -> Path:
        """Generate a preview with metadata for testing retrieval."""
        import asyncio

        request = GeneratePreviewRequest(
            macro_path=test_macro_path,
            artifact_provider_name="Image",
            format=None,
            generate_preview_metadata_json=True,
            preview_generator_parameters={"max_width": 50, "max_height": 50},
        )

        result = asyncio.run(artifact_manager.on_handle_generate_preview_request(request))
        assert isinstance(result, GeneratePreviewResultSuccess)

        # Return the actual preview path from the result
        assert isinstance(result.paths_to_preview, str)
        return Path(result.paths_to_preview)

    @pytest.mark.usefixtures("generated_preview_with_metadata")
    def test_get_preview_success(self, artifact_manager: ArtifactManager, test_macro_path: MacroPath) -> None:
        """Test retrieving existing preview."""
        import asyncio

        request = GetPreviewForArtifactRequest(
            macro_path=test_macro_path,
            artifact_provider_name="Image",
        )

        result = asyncio.run(artifact_manager.on_handle_get_preview_for_artifact_request(request))

        assert isinstance(result, GetPreviewForArtifactResultSuccess)
        assert result.paths_to_preview is not None
        assert isinstance(result.paths_to_preview, str)
        assert Path(result.paths_to_preview).exists()
        assert Path(result.paths_to_preview).is_absolute()

    def test_get_preview_source_file_not_found(self, artifact_manager: ArtifactManager, temp_dir: Path) -> None:
        """Test getting preview for non-existent source file."""
        import asyncio

        nonexistent_path = temp_dir / "nonexistent.jpg"
        parsed_macro = ParsedMacro(str(nonexistent_path))
        macro_path = MacroPath(parsed_macro=parsed_macro, variables={})

        request = GetPreviewForArtifactRequest(
            macro_path=macro_path,
            artifact_provider_name="Image",
        )

        result = asyncio.run(artifact_manager.on_handle_get_preview_for_artifact_request(request))

        assert isinstance(result, GetPreviewForArtifactResultFailure)
        assert "source file not found" in str(result.result_details).lower()

    def test_get_preview_metadata_not_found(
        self, artifact_manager: ArtifactManager, test_macro_path: MacroPath
    ) -> None:
        """Test getting preview when metadata doesn't exist."""
        import asyncio

        # Don't generate preview or metadata
        request = GetPreviewForArtifactRequest(
            macro_path=test_macro_path,
            artifact_provider_name="Image",
            preview_generation_policy=PreviewGenerationPolicy.DO_NOT_GENERATE,
        )

        result = asyncio.run(artifact_manager.on_handle_get_preview_for_artifact_request(request))

        assert isinstance(result, GetPreviewForArtifactResultFailure)
        assert "metadata file not found" in str(result.result_details).lower()

    @pytest.mark.usefixtures("generated_preview_with_metadata")
    def test_get_preview_metadata_malformed_json(
        self,
        artifact_manager: ArtifactManager,
        test_macro_path: MacroPath,
        test_image_path: Path,
    ) -> None:
        """Test getting preview when metadata JSON is malformed."""
        import asyncio

        # Corrupt the metadata file (named after source file, not preview)
        preview_dir = test_image_path.parent / ".griptape-nodes-previews"
        metadata_path = preview_dir / f"{test_image_path.name}.json"
        metadata_path.write_text("{ invalid json }")

        request = GetPreviewForArtifactRequest(
            macro_path=test_macro_path,
            artifact_provider_name="Image",
            preview_generation_policy=PreviewGenerationPolicy.DO_NOT_GENERATE,
        )

        result = asyncio.run(artifact_manager.on_handle_get_preview_for_artifact_request(request))

        assert isinstance(result, GetPreviewForArtifactResultFailure)
        assert "malformed" in str(result.result_details).lower() or "json" in str(result.result_details).lower()

    @pytest.mark.usefixtures("generated_preview_with_metadata")
    def test_get_preview_metadata_invalid_schema(
        self,
        artifact_manager: ArtifactManager,
        test_macro_path: MacroPath,
        test_image_path: Path,
    ) -> None:
        """Test getting preview when metadata has missing required field."""
        import asyncio

        # Write metadata with missing field (named after source file)
        preview_dir = test_image_path.parent / ".griptape-nodes-previews"
        metadata_path = preview_dir / f"{test_image_path.name}.json"
        incomplete_metadata = {
            "version": "0.1.0",
            "source_macro_path": str(test_image_path),
            # Missing: source_file_size, source_file_modified_time, preview_file_name
        }
        metadata_path.write_text(json.dumps(incomplete_metadata))

        request = GetPreviewForArtifactRequest(
            macro_path=test_macro_path,
            artifact_provider_name="Image",
            preview_generation_policy=PreviewGenerationPolicy.DO_NOT_GENERATE,
        )

        result = asyncio.run(artifact_manager.on_handle_get_preview_for_artifact_request(request))

        assert isinstance(result, GetPreviewForArtifactResultFailure)
        assert "invalid metadata" in str(result.result_details).lower()

    @pytest.mark.usefixtures("generated_preview_with_metadata")
    def test_get_preview_stale_source_modified(
        self,
        artifact_manager: ArtifactManager,
        test_macro_path: MacroPath,
        test_image_path: Path,
    ) -> None:
        """Test getting preview when source file was modified."""
        import asyncio

        # Modify source file by writing more data
        with test_image_path.open("ab") as f:
            f.write(b"extra data to change size")

        request = GetPreviewForArtifactRequest(
            macro_path=test_macro_path,
            artifact_provider_name="Image",
            preview_generation_policy=PreviewGenerationPolicy.DO_NOT_GENERATE,
        )

        result = asyncio.run(artifact_manager.on_handle_get_preview_for_artifact_request(request))

        assert isinstance(result, GetPreviewForArtifactResultFailure)
        assert "stale" in str(result.result_details).lower() or "modified" in str(result.result_details).lower()

    @pytest.mark.usefixtures("generated_preview_with_metadata")
    def test_get_preview_stale_source_touched(
        self,
        artifact_manager: ArtifactManager,
        test_macro_path: MacroPath,
        test_image_path: Path,
    ) -> None:
        """Test getting preview when source file mtime was updated."""
        import asyncio
        import time

        future_time = time.time() + 100
        os.utime(test_image_path, (future_time, future_time))

        request = GetPreviewForArtifactRequest(
            macro_path=test_macro_path,
            artifact_provider_name="Image",
            preview_generation_policy=PreviewGenerationPolicy.DO_NOT_GENERATE,
        )

        result = asyncio.run(artifact_manager.on_handle_get_preview_for_artifact_request(request))

        assert isinstance(result, GetPreviewForArtifactResultFailure)
        assert "stale" in str(result.result_details).lower() or "modified" in str(result.result_details).lower()

    def test_get_preview_preview_file_missing(
        self,
        artifact_manager: ArtifactManager,
        test_macro_path: MacroPath,
        generated_preview_with_metadata: Path,
    ) -> None:
        """Test getting preview when preview file is deleted but metadata exists."""
        import asyncio

        # Delete the preview file but keep metadata
        generated_preview_with_metadata.unlink()

        request = GetPreviewForArtifactRequest(
            macro_path=test_macro_path,
            artifact_provider_name="Image",
            preview_generation_policy=PreviewGenerationPolicy.DO_NOT_GENERATE,
        )

        result = asyncio.run(artifact_manager.on_handle_get_preview_for_artifact_request(request))

        assert isinstance(result, GetPreviewForArtifactResultFailure)
        assert "not found" in str(result.result_details).lower()

    def test_get_preview_policy_do_not_generate(
        self, artifact_manager: ArtifactManager, test_macro_path: MacroPath
    ) -> None:
        """Test that DO_NOT_GENERATE policy is accepted and fails when no preview exists."""
        import asyncio

        request = GetPreviewForArtifactRequest(
            macro_path=test_macro_path,
            artifact_provider_name="Image",
            preview_generation_policy=PreviewGenerationPolicy.DO_NOT_GENERATE,
        )

        # Should not raise error (but will fail since no preview exists)
        result = asyncio.run(artifact_manager.on_handle_get_preview_for_artifact_request(request))
        assert isinstance(result, GetPreviewForArtifactResultFailure)

    @pytest.mark.usefixtures("generated_preview_with_metadata")
    def test_get_preview_only_if_stale_regenerates(
        self,
        artifact_manager: ArtifactManager,
        test_macro_path: MacroPath,
        test_image_path: Path,
    ) -> None:
        """Test ONLY_IF_STALE policy regenerates when source is stale."""
        import asyncio

        # Make source stale by modifying it
        with test_image_path.open("ab") as f:
            f.write(b"extra data to change size")

        request = GetPreviewForArtifactRequest(
            macro_path=test_macro_path,
            artifact_provider_name="Image",
            preview_generation_policy=PreviewGenerationPolicy.ONLY_IF_STALE,
        )

        result = asyncio.run(artifact_manager.on_handle_get_preview_for_artifact_request(request))

        # Should regenerate successfully
        assert isinstance(result, GetPreviewForArtifactResultSuccess)
        assert result.paths_to_preview is not None

    def test_get_preview_missing_metadata_with_only_if_stale(
        self, artifact_manager: ArtifactManager, test_macro_path: MacroPath
    ) -> None:
        """Test ONLY_IF_STALE policy generates when metadata missing."""
        import asyncio

        request = GetPreviewForArtifactRequest(
            macro_path=test_macro_path,
            artifact_provider_name="Image",
            preview_generation_policy=PreviewGenerationPolicy.ONLY_IF_STALE,
        )

        result = asyncio.run(artifact_manager.on_handle_get_preview_for_artifact_request(request))

        # Should generate successfully
        assert isinstance(result, GetPreviewForArtifactResultSuccess)
        assert result.paths_to_preview is not None

    @pytest.mark.usefixtures("generated_preview_with_metadata")
    def test_get_preview_always_regenerates(
        self,
        artifact_manager: ArtifactManager,
        test_macro_path: MacroPath,
    ) -> None:
        """Test ALWAYS policy regenerates even when preview is fresh."""
        import asyncio

        request = GetPreviewForArtifactRequest(
            macro_path=test_macro_path,
            artifact_provider_name="Image",
            preview_generation_policy=PreviewGenerationPolicy.ALWAYS,
        )

        result = asyncio.run(artifact_manager.on_handle_get_preview_for_artifact_request(request))

        # Should regenerate successfully even though preview was fresh
        assert isinstance(result, GetPreviewForArtifactResultSuccess)
        assert result.paths_to_preview is not None


class TestGeneratorValidation:
    """Test generator parameter validation logic."""

    def test_pil_thumbnail_validate_parameters_valid(self) -> None:
        """Test PILThumbnailGenerator validates correct parameters."""
        from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators import (
            PILThumbnailGenerator,
        )

        params = {"max_width": 1024, "max_height": 768}
        params_model_class = PILThumbnailGenerator.get_parameters()
        # Should not raise ValidationError
        params_model_class.model_validate(params)

    def test_pil_thumbnail_validate_parameters_invalid_type(self) -> None:
        """Test PILThumbnailGenerator rejects invalid types."""
        from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators import (
            PILThumbnailGenerator,
        )

        params = {"max_width": "not_a_number", "max_height": 768}
        params_model_class = PILThumbnailGenerator.get_parameters()

        with pytest.raises(ValidationError) as exc_info:
            params_model_class.model_validate(params)

        errors = exc_info.value.errors()
        assert len(errors) >= 1
        assert any(e["loc"][0] == "max_width" for e in errors)

    def test_pil_thumbnail_validate_parameters_invalid_value(self) -> None:
        """Test PILThumbnailGenerator rejects negative values."""
        from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators import (
            PILThumbnailGenerator,
        )

        params = {"max_width": -100, "max_height": 768}
        params_model_class = PILThumbnailGenerator.get_parameters()

        with pytest.raises(ValidationError) as exc_info:
            params_model_class.model_validate(params)

        errors = exc_info.value.errors()
        assert len(errors) >= 1
        assert any(e["loc"][0] == "max_width" and "greater_than" in e["type"] for e in errors)

    def test_pil_thumbnail_validate_parameters_missing_key(self) -> None:
        """Test PILThumbnailGenerator uses defaults for missing parameters."""
        from typing import cast

        from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators import (
            PILThumbnailGenerator,
        )

        params = {"max_width": 1024}
        params_model_class = PILThumbnailGenerator.get_parameters()

        # With Pydantic, fields with defaults are optional - should use default value
        validated_params = cast("PILThumbnailParameters", params_model_class.model_validate(params))
        assert validated_params.max_width == 1024  # noqa: PLR2004
        assert validated_params.max_height == 1024  # Uses default value  # noqa: PLR2004

    def test_pil_thumbnail_validate_parameters_extra_key(self) -> None:
        """Test PILThumbnailGenerator ignores unknown parameters (backward compatibility)."""
        from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators import (
            PILThumbnailGenerator,
        )

        params = {"max_width": 1024, "max_height": 768, "unknown_param": 42}
        params_model_class = PILThumbnailGenerator.get_parameters()

        # Extra fields are ignored (for backward compatibility with old configs)
        # See https://github.com/griptape-ai/griptape-nodes/issues/3980
        validated_params = cast("PILThumbnailParameters", params_model_class.model_validate(params))

        # Only known fields are included in the model
        assert validated_params.max_width == 1024  # noqa: PLR2004
        assert validated_params.max_height == 768  # noqa: PLR2004
        assert not hasattr(validated_params, "unknown_param")

    def test_pil_rounded_validate_parameters_valid(self) -> None:
        """Test PILRoundedPreviewGenerator validates correct parameters."""
        from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators import (
            PILRoundedPreviewGenerator,
        )

        params = {"max_width": 1024, "max_height": 768, "corner_radius_percent": 2.0}
        params_model_class = PILRoundedPreviewGenerator.get_parameters()
        # Should not raise ValidationError
        params_model_class.model_validate(params)

    def test_pil_rounded_validate_parameters_invalid_corner_radius_percent(self) -> None:
        """Test PILRoundedPreviewGenerator rejects negative corner_radius_percent."""
        from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators import (
            PILRoundedPreviewGenerator,
        )

        params = {"max_width": 1024, "max_height": 768, "corner_radius_percent": -1.0}
        params_model_class = PILRoundedPreviewGenerator.get_parameters()

        with pytest.raises(ValidationError) as exc_info:
            params_model_class.model_validate(params)

        errors = exc_info.value.errors()
        assert len(errors) >= 1
        assert any(e["loc"][0] == "corner_radius_percent" and "greater_than_equal" in e["type"] for e in errors)

    def test_generator_constructor_uses_validation(self) -> None:
        """Test generator constructors use Pydantic validation and raise ValidationError on invalid params."""
        from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators import (
            PILThumbnailGenerator,
        )

        invalid_params = {"max_width": "not_a_number", "max_height": 768}

        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = Path(tmpdir) / "test.png"
            dest_dir = Path(tmpdir) / "preview"

            with pytest.raises(ValidationError):
                PILThumbnailGenerator(
                    source_file_location=str(source_path),
                    preview_format="webp",
                    destination_preview_directory=str(dest_dir),
                    destination_preview_file_name="preview.webp",
                    params=invalid_params,
                )

    @pytest.mark.asyncio
    async def test_get_artifact_schemas_structure(self) -> None:
        """Test that get_artifact_schemas returns properly structured Pydantic model."""
        from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete
        from griptape_nodes.retained_mode.managers.artifact_providers.artifact_schema_models import ArtifactSchemas

        artifact_manager = ArtifactManager()

        # Register default providers (Image provider with its generators)
        await artifact_manager.on_app_initialization_complete(AppInitializationComplete())

        schemas = artifact_manager._get_artifact_schemas()

        # Verify it's the correct model type
        assert isinstance(schemas, ArtifactSchemas)

        # Verify we can serialize to dict
        schemas_dict = schemas.model_dump()
        assert isinstance(schemas_dict, dict)

        # Verify structure
        assert "image" in schemas_dict
        image_schema = schemas_dict["image"]
        assert "preview_generation" in image_schema

        preview_gen = image_schema["preview_generation"]
        assert "preview_format" in preview_gen
        assert "preview_generator" in preview_gen
        assert "preview_generator_configurations" in preview_gen

        # Verify format schema structure
        format_schema = preview_gen["preview_format"]
        assert format_schema["type"] == "string"
        assert "enum" in format_schema
        assert "default" in format_schema
        assert "description" in format_schema
        assert isinstance(format_schema["enum"], list)
        assert format_schema["default"] in format_schema["enum"]

        # Verify generator schema structure
        gen_schema = preview_gen["preview_generator"]
        assert gen_schema["type"] == "string"
        assert "enum" in gen_schema
        assert "default" in gen_schema
        assert gen_schema["default"] in gen_schema["enum"]

        # Verify generator configurations structure
        gen_configs = preview_gen["preview_generator_configurations"]
        assert isinstance(gen_configs, dict)
        assert len(gen_configs) > 0  # At least one generator registered

        # Check parameter schema structure
        first_gen_key = next(iter(gen_configs.keys()))
        first_gen_params = gen_configs[first_gen_key]
        assert isinstance(first_gen_params, dict)

        # All parameters should have type, default, description
        for param_schema in first_gen_params.values():
            assert "type" in param_schema
            assert "default" in param_schema
            assert "description" in param_schema
            assert isinstance(param_schema["type"], str)
            assert isinstance(param_schema["description"], str)


class TestProviderRegistrationConfigLogLevels:
    """Test that config reads during provider registration use DEBUG-level failure logging.

    On a fresh install with no config, reading config values will fail. These failures
    are expected and should not produce ERROR-level logs that alarm users.
    """

    def test_read_generator_config_uses_debug_failure_log_level(self) -> None:
        """Test that _read_generator_config uses failure_log_level=DEBUG in GetConfigCategoryRequest."""
        import logging

        from griptape_nodes.retained_mode.events.config_events import (
            GetConfigCategoryRequest,
        )
        from griptape_nodes.retained_mode.managers.artifact_providers.image.preview_generators import (
            PILThumbnailGenerator,
        )

        manager = ArtifactManager()
        captured_requests: list[RequestPayload] = []

        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        original = GriptapeNodes.handle_request

        def capture_requests(request: RequestPayload) -> ResultPayload:
            captured_requests.append(request)
            return original(request)

        try:
            GriptapeNodes.handle_request = staticmethod(capture_requests)
            manager._read_generator_config(ImageArtifactProvider, PILThumbnailGenerator)
        finally:
            GriptapeNodes.handle_request = original

        category_requests = [r for r in captured_requests if isinstance(r, GetConfigCategoryRequest)]
        assert len(category_requests) == 1
        assert category_requests[0].failure_log_level == logging.DEBUG

    def test_validate_and_write_provider_settings_uses_debug_failure_log_level(self) -> None:
        """Test that _validate_and_write_provider_settings uses failure_log_level=DEBUG."""
        import logging

        from griptape_nodes.retained_mode.events.config_events import (
            GetConfigValueRequest,
        )

        manager = ArtifactManager()
        captured_requests: list[RequestPayload] = []

        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        original = GriptapeNodes.handle_request

        def capture_requests(request: RequestPayload) -> ResultPayload:
            captured_requests.append(request)
            return original(request)

        try:
            GriptapeNodes.handle_request = staticmethod(capture_requests)
            manager._validate_and_write_provider_settings(ImageArtifactProvider)
        finally:
            GriptapeNodes.handle_request = original

        value_requests = [r for r in captured_requests if isinstance(r, GetConfigValueRequest)]
        assert len(value_requests) >= 2  # noqa: PLR2004
        for req in value_requests:
            assert req.failure_log_level == logging.DEBUG

    @pytest.mark.asyncio
    async def test_provider_registration_does_not_log_errors(self, caplog: pytest.LogCaptureFixture) -> None:
        """Test that registering providers on fresh config does not produce ERROR-level logs."""
        import logging

        from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete

        manager = ArtifactManager()

        with caplog.at_level(logging.DEBUG, logger="griptape_nodes"):
            await manager.on_app_initialization_complete(AppInitializationComplete())

        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records == [], (
            f"Provider registration produced ERROR-level logs: {[r.message for r in error_records]}"
        )
