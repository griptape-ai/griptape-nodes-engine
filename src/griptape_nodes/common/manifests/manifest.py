"""Manifest data model.

A manifest is a serializable snapshot of the engine-local resources the engine
currently knows about (libraries and project templates, for now). It is
generated on request and handed to a UI that builds an interface around those
resources.

The manifest intentionally carries only descriptive metadata. It references
resources by the same identifiers the engine already uses (library names,
project template ids) so consumers can look up live state through the existing
per-resource events.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# Schema version for the manifest envelope. Bump when the shape changes so
# consumers can branch on it.
MANIFEST_SCHEMA_VERSION = "0.1.0"


class LibraryManifestEntry(BaseModel):
    """A single registered library in a manifest.

    Attributes:
        name: Registered library name (the identifier used by library events).
        path: Absolute path to the library's ``griptape_nodes_library.json``,
            when known.
        version: Library version from its metadata, when available.
        author: Library author from its metadata, when available.
        description: Library description from its metadata, when available.
        tags: Search/classification tags declared in the library metadata.
    """

    name: str
    path: str | None = None
    version: str | None = None
    author: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)


class ProjectTemplateManifestEntry(BaseModel):
    """A single loaded project template in a manifest.

    Attributes:
        project_id: Opaque identifier for the template (the identifier used by
            project events and referenced by external consumers such as
            policies). Consumers must not parse or construct it.
        name: Display name from the project template body, when available.
        parent_project_id: Opaque id of the parent project template, matchable
            against another entry's ``project_id``, or None when the template has
            no parent.
        path: Filesystem location of the template file on this engine, or None
            for templates that are not file-backed (e.g. the system defaults).
            Carried separately from ``project_id`` so consumers never have to
            assume the id is a path.
    """

    project_id: str
    name: str | None = None
    parent_project_id: str | None = None
    path: str | None = None


class Manifest(BaseModel):
    """A snapshot of the engine's registered libraries and project templates.

    Attributes:
        schema_version: Version of the manifest envelope.
        generated_at: ISO 8601 timestamp (UTC) of when the manifest was built.
        engine_id: Identifier of the engine that generated this manifest, when
            known. Lets consumers attribute a manifest to its source engine.
        engine_version: Engine version string (``major.minor.patch``), when known.
        libraries: Registered libraries included in this manifest.
        project_templates: Loaded project templates included in this manifest.
    """

    schema_version: str = MANIFEST_SCHEMA_VERSION
    generated_at: str
    engine_id: str | None = None
    engine_version: str | None = None
    libraries: list[LibraryManifestEntry] = Field(default_factory=list)
    project_templates: list[ProjectTemplateManifestEntry] = Field(default_factory=list)
