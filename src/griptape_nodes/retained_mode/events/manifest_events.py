"""Events for generating manifests of engine-local resources."""

from __future__ import annotations

from dataclasses import dataclass

from griptape_nodes.common.manifests.manifest import Manifest
from griptape_nodes.retained_mode.events.base_events import (
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry


@dataclass
@PayloadRegistry.register
class GenerateManifestRequest(RequestPayload):
    """Generate a manifest of the engine's currently registered resources.

    Use when: A UI needs a single descriptive snapshot of the libraries and
    project templates the engine knows about so it can build an interface
    around them.

    The manifest is generated from live engine state and returned in the result.
    Nothing is written to disk or registered.

    Args:
        include_libraries: Include registered libraries in the manifest.
        include_project_templates: Include loaded project templates in the manifest.

    Results: GenerateManifestResultSuccess | GenerateManifestResultFailure
    """

    include_libraries: bool = True
    include_project_templates: bool = True


@dataclass
@PayloadRegistry.register
class GenerateManifestResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Manifest generated successfully.

    Args:
        manifest: The generated manifest describing registered libraries and project templates.
    """

    manifest: Manifest


@dataclass
@PayloadRegistry.register
class GenerateManifestResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Manifest generation failed.

    Common causes: the library registry or project template registry could not be queried.
    """
