from __future__ import annotations

import logging
from dataclasses import dataclass

from griptape_nodes.retained_mode.managers.fitness_problems.libraries.library_problem import LibraryProblem
from griptape_nodes.utils.version_utils import (
    ShadowedPackage,  # noqa: TC001 (serialized dataclass field; a deferred import leaves it unresolvable)
)

logger = logging.getLogger(__name__)

SHADOWED_PACKAGES_DOCS_URL = (
    "https://docs.griptapenodes.com/en/stable/guides/libraries/#a-library-warns-that-it-installs-older-components"
)


@dataclass
class ShadowedEnginePackagesProblem(LibraryProblem):
    """Problem indicating a library's environment holds components older than the engine's own.

    That environment precedes the engine's on the import path, so those are the copies engine code
    binds. The library itself is unaffected, which is why this leaves it registered.
    """

    packages: list[ShadowedPackage]

    @classmethod
    def collate_problems_for_display(cls, instances: list[ShadowedEnginePackagesProblem]) -> str:
        """Display the shadowed components.

        There should only be one instance per library: every shadowed component from one
        installation is reported together.
        """
        if len(instances) > 1:
            logger.error(
                "ShadowedEnginePackagesProblem: Expected 1 instance but got %s. Each LibraryInfo should only have one ShadowedEnginePackagesProblem.",
                len(instances),
            )

        described = ", ".join(
            f"{package.name} {package.library_version} instead of {package.engine_version}"
            for instance in instances
            for package in instance.packages
        )
        return (
            f"Installs components older than the ones Griptape Nodes runs on: {described}. This "
            "library still works, but Griptape Nodes uses those older versions in place of its own, "
            "so features unrelated to this library may break. Reset the library to rebuild its "
            f"environment: {SHADOWED_PACKAGES_DOCS_URL}"
        )
