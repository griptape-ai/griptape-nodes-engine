"""Tests for `validate_library_declarations` and `detect_retired_node_declarations`."""

from __future__ import annotations

from typing import Any

from griptape_nodes.node_library.library_declarations import (
    KeySupport,
    Model,
    ModelCatalogLibraryProperty,
    ModelProvider,
    ModelProviderUsageNodeProperty,
    ModelUsageNodeProperty,
)
from griptape_nodes.node_library.library_registry import (
    CategoryDefinition,
    LibraryMetadata,
    LibrarySchema,
    NodeDefinition,
    NodeMetadata,
)
from griptape_nodes.node_library.library_validation import (
    detect_retired_node_declarations,
    validate_library_declarations,
)
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import (
    DuplicateModelIdProblem,
    RetiredNodeDeclarationProblem,
    UnresolvedModelProviderUsageReferenceProblem,
    UnresolvedModelUsageReferenceProblem,
)


def _make_library_metadata(**kwargs: Any) -> LibraryMetadata:
    return LibraryMetadata(
        author="test",
        description="test library",
        library_version="1.0.0",
        engine_version="1.0.0",
        tags=[],
        **kwargs,
    )


def _make_node_metadata(**kwargs: Any) -> NodeMetadata:
    return NodeMetadata(
        category="Test",
        description="test node",
        display_name="TestNode",
        **kwargs,
    )


def _make_schema(
    *,
    library_declarations: list[Any] | None = None,
    nodes: list[NodeDefinition] | None = None,
) -> LibrarySchema:
    lib_kwargs: dict[str, Any] = {}
    if library_declarations is not None:
        lib_kwargs["declarations"] = library_declarations
    return LibrarySchema(
        name="Test Library",
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=_make_library_metadata(**lib_kwargs),
        categories=[{"Test": CategoryDefinition(title="Test", description="test", color="#000", icon="Folder")}],
        nodes=nodes or [],
    )


def _model(display_name: str = "X") -> Model:
    return Model(display_name=display_name, key_support=KeySupport.REQUIRES_CUSTOMER_KEY)


# ---------- No declarations / clean library ----------


class TestCleanLibrary:
    def test_library_with_no_declarations_has_no_problems(self) -> None:
        schema = _make_schema()

        assert validate_library_declarations(schema) == []

    def test_library_with_only_a_clean_catalog_has_no_problems(self) -> None:
        schema = _make_schema(
            library_declarations=[
                ModelCatalogLibraryProperty(
                    providers={"p": ModelProvider(display_name="P", models={"m": _model()})},
                ),
            ],
        )

        assert validate_library_declarations(schema) == []


# ---------- Duplicate model ids ----------


class TestDuplicateModelIds:
    def test_same_id_under_two_providers_is_fatal(self) -> None:
        schema = _make_schema(
            library_declarations=[
                ModelCatalogLibraryProperty(
                    providers={
                        "anthropic": ModelProvider(display_name="Anthropic", models={"shared": _model()}),
                        "kling": ModelProvider(display_name="Kling", models={"shared": _model()}),
                    },
                ),
            ],
        )

        problems = validate_library_declarations(schema)

        duplicates = [p for p in problems if isinstance(p, DuplicateModelIdProblem)]
        assert len(duplicates) == 1
        assert duplicates[0].model_id == "shared"
        assert sorted(duplicates[0].provider_ids) == ["anthropic", "kling"]

    def test_unique_ids_pass(self) -> None:
        schema = _make_schema(
            library_declarations=[
                ModelCatalogLibraryProperty(
                    providers={
                        "anthropic": ModelProvider(display_name="Anthropic", models={"a": _model()}),
                        "kling": ModelProvider(display_name="Kling", models={"b": _model()}),
                    },
                ),
            ],
        )

        problems = validate_library_declarations(schema)

        assert [p for p in problems if isinstance(p, DuplicateModelIdProblem)] == []


# ---------- Unresolved model usage references ----------


class TestUnresolvedModelUsageReferences:
    def test_node_referencing_missing_model_is_fatal(self) -> None:
        schema = _make_schema(
            library_declarations=[
                ModelCatalogLibraryProperty(
                    providers={"p": ModelProvider(display_name="P", models={"m": _model()})},
                ),
            ],
            nodes=[
                NodeDefinition(
                    class_name="UsesMissing",
                    file_path="x.py",
                    metadata=_make_node_metadata(declarations=[ModelUsageNodeProperty(model_ids=["nonexistent"])]),
                ),
            ],
        )

        problems = validate_library_declarations(schema)

        unresolved = [p for p in problems if isinstance(p, UnresolvedModelUsageReferenceProblem)]
        assert len(unresolved) == 1
        assert unresolved[0].class_name == "UsesMissing"
        assert unresolved[0].model_id == "nonexistent"

    def test_node_referencing_existing_model_passes(self) -> None:
        schema = _make_schema(
            library_declarations=[
                ModelCatalogLibraryProperty(
                    providers={"p": ModelProvider(display_name="P", models={"m": _model()})},
                ),
            ],
            nodes=[
                NodeDefinition(
                    class_name="UsesExisting",
                    file_path="x.py",
                    metadata=_make_node_metadata(declarations=[ModelUsageNodeProperty(model_ids=["m"])]),
                ),
            ],
        )

        assert validate_library_declarations(schema) == []

    def test_node_with_no_catalog_and_a_reference_is_fatal(self) -> None:
        schema = _make_schema(
            library_declarations=[],
            nodes=[
                NodeDefinition(
                    class_name="UsesMissing",
                    file_path="x.py",
                    metadata=_make_node_metadata(declarations=[ModelUsageNodeProperty(model_ids=["m"])]),
                ),
            ],
        )

        problems = validate_library_declarations(schema)

        assert len([p for p in problems if isinstance(p, UnresolvedModelUsageReferenceProblem)]) == 1

    def test_multiple_unresolved_references_all_reported(self) -> None:
        schema = _make_schema(
            library_declarations=[],
            nodes=[
                NodeDefinition(
                    class_name="N",
                    file_path="x.py",
                    metadata=_make_node_metadata(declarations=[ModelUsageNodeProperty(model_ids=["a", "b"])]),
                ),
            ],
        )

        problems = validate_library_declarations(schema)

        ids = [p.model_id for p in problems if isinstance(p, UnresolvedModelUsageReferenceProblem)]
        assert sorted(ids) == ["a", "b"]


# ---------- Unresolved provider-usage references ----------


def _catalog_with_one_provider() -> ModelCatalogLibraryProperty:
    return ModelCatalogLibraryProperty(
        providers={"openai": ModelProvider(display_name="OpenAI", models={"openai_gpt_5": _model()})},
    )


class TestUnresolvedProviderUsageReferences:
    def test_node_referencing_existing_provider_passes(self) -> None:
        schema = _make_schema(
            library_declarations=[_catalog_with_one_provider()],
            nodes=[
                NodeDefinition(
                    class_name="UsesOpenai",
                    file_path="x.py",
                    metadata=_make_node_metadata(
                        declarations=[ModelProviderUsageNodeProperty(provider_ids=["openai"])],
                    ),
                ),
            ],
        )

        assert validate_library_declarations(schema) == []

    def test_node_referencing_missing_provider_is_fatal(self) -> None:
        schema = _make_schema(
            library_declarations=[_catalog_with_one_provider()],
            nodes=[
                NodeDefinition(
                    class_name="UsesAcme",
                    file_path="x.py",
                    metadata=_make_node_metadata(
                        declarations=[ModelProviderUsageNodeProperty(provider_ids=["acme"])],
                    ),
                ),
            ],
        )

        problems = validate_library_declarations(schema)

        unresolved = [p for p in problems if isinstance(p, UnresolvedModelProviderUsageReferenceProblem)]
        assert len(unresolved) == 1
        assert unresolved[0].class_name == "UsesAcme"
        assert unresolved[0].provider_id == "acme"

    def test_multiple_unresolved_providers_all_reported(self) -> None:
        schema = _make_schema(
            library_declarations=[_catalog_with_one_provider()],
            nodes=[
                NodeDefinition(
                    class_name="N",
                    file_path="x.py",
                    metadata=_make_node_metadata(
                        declarations=[ModelProviderUsageNodeProperty(provider_ids=["acme", "wile_e"])],
                    ),
                ),
            ],
        )

        problems = validate_library_declarations(schema)

        ids = [p.provider_id for p in problems if isinstance(p, UnresolvedModelProviderUsageReferenceProblem)]
        assert sorted(ids) == ["acme", "wile_e"]


# ---------- Retired node declarations (raw JSON scan) ----------


class TestDetectRetiredNodeDeclarations:
    def test_retired_key_support_is_detected(self) -> None:
        library_json = {
            "name": "Old Library",
            "nodes": [
                {
                    "class_name": "OldNode",
                    "metadata": {"declarations": [{"type": "key_support", "support": "REQUIRES_CUSTOMER_KEY"}]},
                },
            ],
        }

        problems = detect_retired_node_declarations(library_json)

        assert len(problems) == 1
        problem = problems[0]
        assert isinstance(problem, RetiredNodeDeclarationProblem)
        assert problem.class_name == "OldNode"
        assert problem.declaration_type == "key_support"
        assert problem.guidance

    def test_current_declarations_are_not_flagged(self) -> None:
        library_json = {
            "name": "Current Library",
            "nodes": [
                {
                    "class_name": "CurrentNode",
                    "metadata": {"declarations": [{"type": "model_usage", "model_ids": ["m"]}]},
                },
            ],
        }

        assert detect_retired_node_declarations(library_json) == []

    def test_missing_or_malformed_nodes_are_ignored(self) -> None:
        assert detect_retired_node_declarations({"name": "No Nodes"}) == []
        assert detect_retired_node_declarations({"name": "Bad", "nodes": "not-a-list"}) == []
        assert detect_retired_node_declarations({"name": "Bad", "nodes": [42, {"metadata": None}]}) == []
