from __future__ import annotations

from typing import TYPE_CHECKING

import semver

from griptape_nodes.retained_mode.events.app_events import (
    GetEngineVersionRequest,
    GetEngineVersionResultSuccess,
)
from griptape_nodes.retained_mode.managers.fitness_problems.libraries.modified_parameters_set_deprecation_warning_problem import (
    ModifiedParametersSetDeprecationWarningProblem,
)
from griptape_nodes.retained_mode.managers.fitness_problems.libraries.modified_parameters_set_removed_problem import (
    ModifiedParametersSetRemovedProblem,
)
from griptape_nodes.retained_mode.managers.fitness_problems.libraries.ui_options_field_modified_incompatible_problem import (
    UiOptionsFieldModifiedIncompatibleProblem,
)
from griptape_nodes.retained_mode.managers.fitness_problems.libraries.ui_options_field_modified_warning_problem import (
    UiOptionsFieldModifiedWarningProblem,
)
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager
from griptape_nodes.retained_mode.managers.version_compatibility_manager import (
    LibraryVersionCompatibilityCheck,
    LibraryVersionCompatibilityIssue,
)

if TYPE_CHECKING:
    from griptape_nodes.node_library.library_registry import LibrarySchema


class ModifiedParametersSetRemovalCheck(LibraryVersionCompatibilityCheck):
    """Check for libraries impacted by the modified_parameters_set deprecation timeline."""

    def applies_to_library(self, library_data: LibrarySchema) -> bool:
        """Check applies to libraries with engine_version < 0.39.0."""
        try:
            library_version = semver.VersionInfo.parse(library_data.metadata.engine_version)
            return library_version < semver.VersionInfo(0, 39, 0)
        except Exception:
            return False

    def check_library(self, library_data: LibrarySchema) -> list[LibraryVersionCompatibilityIssue]:
        """Perform the modified_parameters_set deprecation check."""
        # Get current engine version
        engine_version_result = self.engine.handle_request(GetEngineVersionRequest())
        if not isinstance(engine_version_result, GetEngineVersionResultSuccess):
            # If we can't get current engine version, skip version-specific warnings
            return []

        current_engine_version = semver.VersionInfo(
            engine_version_result.major, engine_version_result.minor, engine_version_result.patch
        )

        # Determine which phase we're in based on current engine version
        library_version_str = library_data.metadata.engine_version

        if current_engine_version >= semver.VersionInfo(0, 39, 0):
            # 0.39+ Release: Parameter removed, reject incompatible libraries
            return [
                LibraryVersionCompatibilityIssue(
                    problem=ModifiedParametersSetRemovedProblem(library_engine_version=library_version_str),
                    severity=LibraryManager.LibraryFitness.UNUSABLE,
                ),
                LibraryVersionCompatibilityIssue(
                    problem=UiOptionsFieldModifiedIncompatibleProblem(library_engine_version=library_version_str),
                    severity=LibraryManager.LibraryFitness.UNUSABLE,
                ),
            ]
        if current_engine_version >= semver.VersionInfo(0, 38, 0):
            # 0.38 Release: Warning about upcoming removal in 0.39
            return [
                LibraryVersionCompatibilityIssue(
                    problem=ModifiedParametersSetDeprecationWarningProblem(library_engine_version=library_version_str),
                    severity=LibraryManager.LibraryFitness.FLAWED,
                ),
                LibraryVersionCompatibilityIssue(
                    problem=UiOptionsFieldModifiedWarningProblem(),
                    severity=LibraryManager.LibraryFitness.FLAWED,
                ),
            ]

        # No compatibility issues for current version
        return []
