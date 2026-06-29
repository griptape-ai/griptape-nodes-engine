"""Cross-reference validation for declarative library declarations.

After Pydantic shape validation via `LibrarySchema.model_validate()`, references
between declarations (a `ModelUsageNodeProperty.model_ids` entry pointing to a
model id in `ModelCatalogLibraryProperty`) need to be resolved against the
library's own declarations.

`validate_library_declarations` walks a validated `LibrarySchema` and returns
the list of blocking problems found; the caller blocks the library load when
that list is non-empty.

`detect_retired_node_declarations` runs against the *raw* JSON before Pydantic
validation, turning declaration `type` tags that were valid in an older schema
into targeted migration guidance instead of opaque discriminator errors.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

from griptape_nodes.node_library.library_declarations import (
    ModelCatalogLibraryProperty,
    ModelProviderUsageNodeProperty,
    ModelUsageNodeProperty,
    find_model_catalog,
    iter_catalog_models,
)
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import (
    DuplicateModelIdProblem,
    LibraryProblem,
    RetiredNodeDeclarationProblem,
    UnresolvedModelProviderUsageReferenceProblem,
    UnresolvedModelUsageReferenceProblem,
)

if TYPE_CHECKING:
    from griptape_nodes.node_library.library_registry import LibrarySchema

# Node declaration `type` tags removed in a schema version, mapped to migration guidance.
# Detected in the raw JSON so authors get a targeted message instead of a Pydantic
# discriminator error.
RETIRED_NODE_DECLARATION_GUIDANCE: dict[str, str] = {
    "key_support": (
        "Node-level 'key_support' was removed. Declare the models a node uses in a "
        "library-level 'model_catalog' (each model carries its own 'key_support') and "
        "reference them from the node with 'model_usage'."
    ),
}


def validate_library_declarations(library_data: LibrarySchema) -> list[LibraryProblem]:
    """Resolve cross-references within a library.

    Returns every blocking problem found; validation does not short-circuit on
    the first one.
    """
    problems: list[LibraryProblem] = []
    library_name = library_data.name

    catalog = find_model_catalog(library_data.metadata.declarations)
    declared_model_ids: set[str] = set()
    declared_provider_ids: set[str] = set()
    if catalog is not None:
        declared_model_ids = _check_duplicate_model_ids(library_name, catalog, problems)
        declared_provider_ids = set(catalog.providers.keys())

    _check_unresolved_node_references(
        library_name=library_name,
        library_data=library_data,
        declared_model_ids=declared_model_ids,
        declared_provider_ids=declared_provider_ids,
        problems=problems,
    )

    return problems


def detect_retired_node_declarations(library_json: dict[str, Any]) -> list[LibraryProblem]:
    """Scan raw library JSON for node declarations using a retired `type` tag.

    Runs before Pydantic validation so a library written against an older schema
    fails with migration guidance rather than an opaque discriminator error.
    """
    library_name = library_json.get("name", "<unknown>")
    nodes = library_json.get("nodes")
    if not isinstance(nodes, list):
        return []

    problems: list[LibraryProblem] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        class_name = node.get("class_name", "<unknown>")
        metadata = node.get("metadata")
        if not isinstance(metadata, dict):
            continue
        declarations = metadata.get("declarations")
        if not isinstance(declarations, list):
            continue
        for declaration in declarations:
            if not isinstance(declaration, dict):
                continue
            declaration_type = declaration.get("type")
            if not isinstance(declaration_type, str):
                continue
            guidance = RETIRED_NODE_DECLARATION_GUIDANCE.get(declaration_type)
            if guidance is None:
                continue
            problems.append(
                RetiredNodeDeclarationProblem(
                    library_name=library_name,
                    class_name=class_name,
                    declaration_type=declaration_type,
                    guidance=guidance,
                )
            )
    return problems


def _check_duplicate_model_ids(
    library_name: str,
    catalog: ModelCatalogLibraryProperty,
    problems: list[LibraryProblem],
) -> set[str]:
    """Walk the catalog and report model ids declared under more than one provider.

    Returns the set of declared model ids (ignoring duplicates) so the caller can
    validate node references against it.
    """
    providers_by_id: dict[str, list[str]] = defaultdict(list)
    for resolved in iter_catalog_models(catalog):
        providers_by_id[resolved.model_id].append(resolved.provider_id)

    for model_id, provider_ids in providers_by_id.items():
        if len(provider_ids) > 1:
            problems.append(
                DuplicateModelIdProblem(
                    library_name=library_name,
                    model_id=model_id,
                    provider_ids=tuple(provider_ids),
                )
            )

    return set(providers_by_id.keys())


def _check_unresolved_node_references(
    *,
    library_name: str,
    library_data: LibrarySchema,
    declared_model_ids: set[str],
    declared_provider_ids: set[str],
    problems: list[LibraryProblem],
) -> None:
    """Walk every node and validate each model_usage / provider reference."""
    for node_def in library_data.nodes:
        for node_decl in node_def.metadata.declarations:
            if isinstance(node_decl, ModelUsageNodeProperty):
                for model_id in node_decl.model_ids:
                    if model_id in declared_model_ids:
                        continue
                    problems.append(
                        UnresolvedModelUsageReferenceProblem(
                            library_name=library_name,
                            class_name=node_def.class_name,
                            model_id=model_id,
                        )
                    )
            elif isinstance(node_decl, ModelProviderUsageNodeProperty):
                for provider_id in node_decl.provider_ids:
                    if provider_id in declared_provider_ids:
                        continue
                    problems.append(
                        UnresolvedModelProviderUsageReferenceProblem(
                            library_name=library_name,
                            class_name=node_def.class_name,
                            provider_id=provider_id,
                        )
                    )
