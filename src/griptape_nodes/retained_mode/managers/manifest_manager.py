"""ManifestManager - Generates manifests describing engine-local resources."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from griptape_nodes.common.manifests.manifest import (
    LibraryManifestEntry,
    Manifest,
    ModelManifestEntry,
    ModelProviderManifestEntry,
    ProjectTemplateManifestEntry,
)
from griptape_nodes.node_library.library_declarations import ModelCatalogLibraryProperty
from griptape_nodes.retained_mode.events.app_events import (
    GetEngineVersionRequest,
    GetEngineVersionResultSuccess,
)
from griptape_nodes.retained_mode.events.library_events import (
    GetLibraryMetadataRequest,
    GetLibraryMetadataResultSuccess,
    GetLibrarySourceInfoRequest,
    GetLibrarySourceInfoResultSuccess,
    ListRegisteredLibrariesRequest,
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
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.events.project_events import ProjectTemplateInfo
    from griptape_nodes.retained_mode.managers.event_manager import EventManager

logger = logging.getLogger("griptape_nodes")


class ManifestManager:
    """Builds manifests describing the engine's registered libraries and project templates.

    The manager owns no persistent state. It assembles a manifest on demand by
    querying the existing library and project registries through their events.
    """

    def __init__(self, event_manager: EventManager) -> None:
        """Initialize the ManifestManager.

        Args:
            event_manager: The EventManager instance to use for event handling.
        """
        event_manager.assign_manager_to_request_type(GenerateManifestRequest, self.on_generate_manifest_request)

    async def on_generate_manifest_request(
        self, request: GenerateManifestRequest
    ) -> GenerateManifestResultSuccess | GenerateManifestResultFailure:
        """Build a manifest from the engine's currently registered resources."""
        # Libraries are listed once when either the library entries or the model
        # catalog (aggregated from library declarations) is requested.
        library_names: list[str] = []
        if request.include_libraries or request.include_model_catalog:
            list_result = await GriptapeNodes.ahandle_request(ListRegisteredLibrariesRequest(broadcast_result=False))
            if not isinstance(list_result, ListRegisteredLibrariesResultSuccess):
                return GenerateManifestResultFailure(
                    result_details=f"Attempted to generate manifest. Failed to list registered libraries: {list_result.result_details}"
                )
            library_names = list_result.libraries

        libraries: list[LibraryManifestEntry] = []
        if request.include_libraries:
            libraries = await self._build_library_entries(library_names)

        model_providers: list[ModelProviderManifestEntry] = []
        models: list[ModelManifestEntry] = []
        if request.include_model_catalog:
            model_providers, models = await self._build_model_catalog_entries(library_names)

        projects: list[ProjectTemplateManifestEntry] = []
        if request.include_project_templates:
            # System builtins (the always-loaded default project) are included:
            # they are real templates an engine can be working in, so consumers
            # picking from the manifest must be able to reference them.
            projects_result = await GriptapeNodes.ahandle_request(
                ListProjectTemplatesRequest(include_system_builtins=True, broadcast_result=False)
            )
            if not isinstance(projects_result, ListProjectTemplatesResultSuccess):
                return GenerateManifestResultFailure(
                    result_details=f"Attempted to generate manifest. Failed to list project templates: {projects_result.result_details}"
                )
            projects = self._build_project_template_entries(projects_result.successfully_loaded)

        manifest = Manifest(
            generated_at=datetime.now(UTC).isoformat(),
            engine_id=self._resolve_engine_id(),
            engine_version=await self._resolve_engine_version(),
            libraries=libraries,
            project_templates=projects,
            model_providers=model_providers,
            models=models,
        )

        return GenerateManifestResultSuccess(
            manifest=manifest,
            result_details=(
                f"Successfully generated manifest with {len(libraries)} library/libraries, "
                f"{len(projects)} project template(s), and {len(model_providers)} model provider(s)."
            ),
        )

    async def _build_library_entries(self, library_names: list[str]) -> list[LibraryManifestEntry]:
        """Build a manifest entry for each registered library.

        A library whose metadata or source path cannot be resolved is still
        included with whatever could be gathered, so one bad library does not
        omit it from the manifest.
        """
        entries: list[LibraryManifestEntry] = []
        for name in library_names:
            metadata_result = await GriptapeNodes.ahandle_request(
                GetLibraryMetadataRequest(library=name, broadcast_result=False)
            )
            metadata = None
            if isinstance(metadata_result, GetLibraryMetadataResultSuccess):
                metadata = metadata_result.metadata
            else:
                logger.warning(
                    "Could not load metadata for library '%s' while generating manifest: %s",
                    name,
                    metadata_result.result_details,
                )

            source_result = await GriptapeNodes.ahandle_request(
                GetLibrarySourceInfoRequest(library=name, broadcast_result=False)
            )
            path = None
            if isinstance(source_result, GetLibrarySourceInfoResultSuccess):
                path = source_result.library_json_path
            else:
                logger.warning(
                    "Could not resolve source path for library '%s' while generating manifest: %s",
                    name,
                    source_result.result_details,
                )

            entries.append(
                LibraryManifestEntry(
                    name=name,
                    path=path,
                    version=metadata.library_version if metadata else None,
                    author=metadata.author if metadata else None,
                    description=metadata.description if metadata else None,
                    tags=list(metadata.tags) if metadata else [],
                )
            )
        return entries

    async def _build_model_catalog_entries(
        self, library_names: list[str]
    ) -> tuple[list[ModelProviderManifestEntry], list[ModelManifestEntry]]:
        """Aggregate the model catalog declared across the registered libraries.

        Each library may declare a ``ModelCatalogLibraryProperty`` (providers ->
        models). Providers are deduplicated by ``provider_id`` and models by
        ``model_id`` (first declaration wins), so the manifest carries one entry
        per handle even when several libraries declare overlapping catalogs. A
        library whose metadata cannot be resolved is skipped with a warning
        rather than failing the whole manifest. Entries are sorted by id for
        stable output.
        """
        providers: dict[str, ModelProviderManifestEntry] = {}
        models: dict[str, ModelManifestEntry] = {}
        for name in library_names:
            metadata_result = await GriptapeNodes.ahandle_request(
                GetLibraryMetadataRequest(library=name, broadcast_result=False)
            )
            if not isinstance(metadata_result, GetLibraryMetadataResultSuccess):
                logger.warning(
                    "Could not load metadata for library '%s' while aggregating the model catalog: %s",
                    name,
                    metadata_result.result_details,
                )
                continue
            for declaration in metadata_result.metadata.declarations:
                if not isinstance(declaration, ModelCatalogLibraryProperty):
                    continue
                for provider_id, provider in declaration.providers.items():
                    providers.setdefault(
                        provider_id,
                        ModelProviderManifestEntry(
                            provider_id=provider_id,
                            display_name=provider.display_name,
                            terms_url=provider.terms_url,
                        ),
                    )
                    for model_id, model in provider.models.items():
                        models.setdefault(
                            model_id,
                            ModelManifestEntry(
                                model_id=model_id,
                                provider_id=provider_id,
                                display_name=model.display_name,
                                family=model.family,
                                terms_url=model.terms_url,
                            ),
                        )
        sorted_providers = [providers[key] for key in sorted(providers)]
        sorted_models = [models[key] for key in sorted(models)]
        return sorted_providers, sorted_models

    def _build_project_template_entries(
        self, project_infos: list[ProjectTemplateInfo]
    ) -> list[ProjectTemplateManifestEntry]:
        """Build a manifest entry for each successfully loaded project template.

        ``path`` is populated from the template's file locator. System builtins
        and other non-file-backed templates have no locator, so their ``path``
        stays None. ``path`` is carried separately from ``project_id`` because the
        id is opaque (a GUID or custom string) and must not be assumed to be a path.
        """
        entries: list[ProjectTemplateManifestEntry] = []
        for info in project_infos:
            entries.append(  # noqa: PERF401
                ProjectTemplateManifestEntry(
                    project_id=info.project_id,
                    name=info.name,
                    parent_project_id=info.parent_project_id,
                    path=info.project_file_path,
                )
            )
        return entries

    def _resolve_engine_id(self) -> str | None:
        """Resolve the engine's identifier, or None when it cannot be determined."""
        try:
            return GriptapeNodes.EngineIdentityManager().engine_id
        except Exception:
            logger.warning("Could not resolve engine id while generating manifest.", exc_info=True)
            return None

    async def _resolve_engine_version(self) -> str | None:
        """Resolve the engine version string, or None when it cannot be determined."""
        version_result = await GriptapeNodes.ahandle_request(GetEngineVersionRequest(broadcast_result=False))
        if isinstance(version_result, GetEngineVersionResultSuccess):
            return f"{version_result.major}.{version_result.minor}.{version_result.patch}"
        logger.warning(
            "Could not resolve engine version while generating manifest: %s",
            version_result.result_details,
        )
        return None
