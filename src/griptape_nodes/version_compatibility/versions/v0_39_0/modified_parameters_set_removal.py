from __future__ import annotations

from typing import TYPE_CHECKING

import semver

from griptape_nodes.retained_mode.managers.fitness_problems.libraries.modified_parameters_set_removed_problem import (
    ModifiedParametersSetRemovedProblem,
)
from griptape_nodes.retained_mode.managers.fitness_problems.libraries.ui_options_field_modified_incompatible_problem import (
    UiOptionsFieldModifiedIncompatibleProblem,
)
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager
from griptape_nodes.retained_mode.managers.version_compatibility_manager import (
    LibraryVersionCompatibilityCheck,
    LibraryVersionCompatibilityIssue,
)

if TYPE_CHECKING:
    from griptape_nodes.node_library.library_registry import LibrarySchema


class ModifiedParametersSetRemovalCheck(LibraryVersionCompatibilityCheck):
    """Reject libraries built before 0.39.0 removed modified_parameters_set and the ui_options field."""

    def applies_to_library(self, library_data: LibrarySchema) -> bool:
        """Check applies to libraries with engine_version < 0.39.0."""
        try:
            library_version = semver.VersionInfo.parse(library_data.metadata.engine_version)
            return library_version < semver.VersionInfo(0, 39, 0)
        except Exception:
            return False

    def check_library(self, library_data: LibrarySchema) -> list[LibraryVersionCompatibilityIssue]:
        """Mark the library unusable."""
        library_version_str = library_data.metadata.engine_version
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
