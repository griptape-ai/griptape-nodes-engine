"""Tests for ManifestManager.on_generate_manifest_request."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.node_library.library_registry import LibraryMetadata
from griptape_nodes.retained_mode.events.app_events import GetEngineVersionResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    GetLibraryMetadataResultFailure,
    GetLibraryMetadataResultSuccess,
    GetLibrarySourceInfoResultSuccess,
    ListRegisteredLibrariesResultFailure,
    ListRegisteredLibrariesResultSuccess,
)
from griptape_nodes.retained_mode.events.manifest_events import (
    GenerateManifestRequest,
    GenerateManifestResultFailure,
    GenerateManifestResultSuccess,
)
from griptape_nodes.retained_mode.events.project_events import (
    ListProjectTemplatesRequest,
    ListProjectTemplatesResultSuccess,
    ProjectTemplateInfo,
)
from griptape_nodes.retained_mode.managers.manifest_manager import ManifestManager
from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY

MODULE_PATH = "griptape_nodes.retained_mode.managers.manifest_manager"


def _library_metadata() -> LibraryMetadata:
    return LibraryMetadata(
        author="Author A",
        description="Library A description",
        library_version="1.2.3",
        engine_version="0.86.0",
        tags=["image", "demo"],
    )


def _project_info() -> ProjectTemplateInfo:
    info = MagicMock(spec=ProjectTemplateInfo)
    info.project_id = "/workspace/projectA/griptape-nodes-project.yml"
    info.name = "Project A"
    info.project_file_path = "/workspace/projectA/griptape-nodes-project.yml"
    info.parent_project_id = None
    return info


class TestManifestManager:
    """Tests for manifest generation."""

    @pytest.fixture
    def manifest_manager(self) -> ManifestManager:
        return ManifestManager(MagicMock())

    @pytest.mark.asyncio
    async def test_generate_manifest_includes_libraries_and_project_templates(
        self, manifest_manager: ManifestManager
    ) -> None:
        """A full run includes libraries, project templates, engine id, and engine version."""
        with patch(f"{MODULE_PATH}.GriptapeNodes") as mock_gn:
            mock_gn.EngineIdentityManager.return_value.engine_id = "engine-uuid-1"
            mock_gn.ahandle_request = AsyncMock(
                side_effect=[
                    ListRegisteredLibrariesResultSuccess(libraries=["Lib A"], result_details="ok"),
                    GetLibraryMetadataResultSuccess(metadata=_library_metadata(), result_details="ok"),
                    GetLibrarySourceInfoResultSuccess(
                        library_name="Lib A",
                        library_json_path="/libs/a/griptape_nodes_library.json",
                        library_directory="/libs/a",
                        result_details="ok",
                    ),
                    ListProjectTemplatesResultSuccess(
                        successfully_loaded=[_project_info()],
                        failed_to_load=[],
                        result_details="ok",
                    ),
                    GetEngineVersionResultSuccess(major=1, minor=2, patch=3, result_details="ok"),
                ]
            )

            result = await manifest_manager.on_generate_manifest_request(
                GenerateManifestRequest(include_model_catalog=False)
            )

        assert isinstance(result, GenerateManifestResultSuccess)
        manifest = result.manifest
        assert manifest.engine_id == "engine-uuid-1"
        assert manifest.engine_version == "1.2.3"
        assert manifest.generated_at  # populated with an ISO timestamp

        # Project templates are requested with system builtins included, so the
        # always-loaded default project is part of the manifest.
        list_templates_request = mock_gn.ahandle_request.await_args_list[3].args[0]
        assert isinstance(list_templates_request, ListProjectTemplatesRequest)
        assert list_templates_request.include_system_builtins is True

        assert len(manifest.libraries) == 1
        library = manifest.libraries[0]
        assert library.name == "Lib A"
        assert library.path == "/libs/a/griptape_nodes_library.json"
        assert library.version == "1.2.3"
        assert library.author == "Author A"
        assert library.description == "Library A description"
        assert library.tags == ["image", "demo"]

        assert len(manifest.project_templates) == 1
        project = manifest.project_templates[0]
        assert project.project_id == "/workspace/projectA/griptape-nodes-project.yml"
        assert project.name == "Project A"
        assert project.parent_project_id is None
        assert project.path == "/workspace/projectA/griptape-nodes-project.yml"

    @pytest.mark.asyncio
    async def test_generate_manifest_fails_when_library_listing_fails(self, manifest_manager: ManifestManager) -> None:
        """A failure listing libraries surfaces as a manifest generation failure."""
        with patch(f"{MODULE_PATH}.GriptapeNodes") as mock_gn:
            mock_gn.ahandle_request = AsyncMock(
                side_effect=[ListRegisteredLibrariesResultFailure(result_details="registry down")]
            )

            result = await manifest_manager.on_generate_manifest_request(
                GenerateManifestRequest(include_model_catalog=False)
            )

        assert isinstance(result, GenerateManifestResultFailure)

    @pytest.mark.asyncio
    async def test_generate_manifest_includes_library_with_partial_metadata(
        self, manifest_manager: ManifestManager
    ) -> None:
        """A library whose metadata fails is still included with what could be gathered."""
        with patch(f"{MODULE_PATH}.GriptapeNodes") as mock_gn:
            mock_gn.EngineIdentityManager.return_value.engine_id = "engine-uuid-1"
            mock_gn.ahandle_request = AsyncMock(
                side_effect=[
                    ListRegisteredLibrariesResultSuccess(libraries=["Lib A"], result_details="ok"),
                    GetLibraryMetadataResultFailure(result_details="not loaded"),
                    GetLibrarySourceInfoResultSuccess(
                        library_name="Lib A",
                        library_json_path="/libs/a/griptape_nodes_library.json",
                        library_directory="/libs/a",
                        result_details="ok",
                    ),
                    ListProjectTemplatesResultSuccess(
                        successfully_loaded=[],
                        failed_to_load=[],
                        result_details="ok",
                    ),
                    GetEngineVersionResultSuccess(major=1, minor=2, patch=3, result_details="ok"),
                ]
            )

            result = await manifest_manager.on_generate_manifest_request(
                GenerateManifestRequest(include_model_catalog=False)
            )

        assert isinstance(result, GenerateManifestResultSuccess)
        assert len(result.manifest.libraries) == 1
        library = result.manifest.libraries[0]
        assert library.name == "Lib A"
        assert library.path == "/libs/a/griptape_nodes_library.json"
        assert library.version is None
        assert library.author is None
        assert library.tags == []

    @pytest.mark.asyncio
    async def test_generate_manifest_includes_system_default_template(self, manifest_manager: ManifestManager) -> None:
        """The system defaults template is included, with no path (it is not file-backed)."""
        default_info = MagicMock(spec=ProjectTemplateInfo)
        default_info.project_id = SYSTEM_DEFAULTS_KEY
        default_info.name = "Default Project"
        default_info.project_file_path = None
        default_info.parent_project_id = None

        with patch(f"{MODULE_PATH}.GriptapeNodes") as mock_gn:
            mock_gn.EngineIdentityManager.return_value.engine_id = "engine-uuid-1"
            mock_gn.ahandle_request = AsyncMock(
                side_effect=[
                    ListProjectTemplatesResultSuccess(
                        successfully_loaded=[default_info],
                        failed_to_load=[],
                        result_details="ok",
                    ),
                    GetEngineVersionResultSuccess(major=1, minor=2, patch=3, result_details="ok"),
                ]
            )

            result = await manifest_manager.on_generate_manifest_request(
                GenerateManifestRequest(include_libraries=False, include_model_catalog=False)
            )

        assert isinstance(result, GenerateManifestResultSuccess)
        assert len(result.manifest.project_templates) == 1
        template = result.manifest.project_templates[0]
        assert template.project_id == SYSTEM_DEFAULTS_KEY
        assert template.name == "Default Project"
        assert template.path is None

    @pytest.mark.asyncio
    async def test_generate_manifest_respects_toggles(self, manifest_manager: ManifestManager) -> None:
        """Disabling both toggles only resolves the engine version."""
        with patch(f"{MODULE_PATH}.GriptapeNodes") as mock_gn:
            mock_gn.EngineIdentityManager.return_value.engine_id = "engine-uuid-1"
            mock_gn.ahandle_request = AsyncMock(
                side_effect=[GetEngineVersionResultSuccess(major=2, minor=0, patch=0, result_details="ok")]
            )

            result = await manifest_manager.on_generate_manifest_request(
                GenerateManifestRequest(
                    include_libraries=False, include_project_templates=False, include_model_catalog=False
                )
            )

        assert isinstance(result, GenerateManifestResultSuccess)
        assert result.manifest.libraries == []
        assert result.manifest.project_templates == []
        assert result.manifest.engine_version == "2.0.0"
        assert mock_gn.ahandle_request.await_count == 1

    @pytest.mark.asyncio
    async def test_generate_manifest_aggregates_model_catalog(self, manifest_manager: ManifestManager) -> None:
        """The model catalog is aggregated from loaded libraries' declarations."""
        from griptape_nodes.node_library.library_declarations import (
            KeySupport,
            Model,
            ModelCatalogLibraryProperty,
            ModelProvider,
        )

        metadata = LibraryMetadata(
            author="a",
            description="d",
            library_version="1.0.0",
            engine_version="0.86.0",
            tags=[],
            declarations=[
                ModelCatalogLibraryProperty(
                    providers={
                        "anthropic": ModelProvider(
                            display_name="Anthropic",
                            terms_url="https://anthropic.com/terms",
                            models={
                                "claude-opus-4": Model(
                                    display_name="Claude Opus 4",
                                    family="Claude 4",
                                    key_support=KeySupport.REQUIRES_CUSTOMER_KEY,
                                    terms_url="https://anthropic.com/model-terms",
                                )
                            },
                        )
                    }
                )
            ],
        )

        with patch(f"{MODULE_PATH}.GriptapeNodes") as mock_gn:
            mock_gn.EngineIdentityManager.return_value.engine_id = "engine-uuid-1"
            mock_gn.ahandle_request = AsyncMock(
                side_effect=[
                    ListRegisteredLibrariesResultSuccess(libraries=["Lib A"], result_details="ok"),
                    GetLibraryMetadataResultSuccess(metadata=metadata, result_details="ok"),
                    GetEngineVersionResultSuccess(major=1, minor=0, patch=0, result_details="ok"),
                ]
            )

            result = await manifest_manager.on_generate_manifest_request(
                GenerateManifestRequest(include_libraries=False, include_project_templates=False)
            )

        assert isinstance(result, GenerateManifestResultSuccess)
        assert [provider.provider_id for provider in result.manifest.model_providers] == ["anthropic"]
        assert result.manifest.model_providers[0].display_name == "Anthropic"
        assert result.manifest.model_providers[0].terms_url == "https://anthropic.com/terms"
        assert [model.model_id for model in result.manifest.models] == ["claude-opus-4"]
        model = result.manifest.models[0]
        assert model.provider_id == "anthropic"
        assert model.display_name == "Claude Opus 4"
        assert model.family == "Claude 4"
        assert model.terms_url == "https://anthropic.com/model-terms"
