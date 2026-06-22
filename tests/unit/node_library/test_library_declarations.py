"""Tests for LibraryDeclaration / NodeDeclaration discriminated unions."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from griptape_nodes.node_library.library_declarations import (
    ArbitraryPythonExecutionNodeProperty,
    KeySupport,
    LifecycleStage,
    LifecycleStageLibraryProperty,
    LifecycleStageNodeProperty,
    Model,
    ModelCatalogLibraryProperty,
    ModelProvider,
    ModelProviderUsageNodeProperty,
    ModelUsageNodeProperty,
    SuggestedWorkerMode,
    WorkerCompatibility,
    WorkerMode,
    WorkerModeCompatibility,
    iter_catalog_models,
    requires_worker_process,
    resolve_node_models,
)
from griptape_nodes.node_library.library_registry import (
    CategoryDefinition,
    LibraryMetadata,
    LibrarySchema,
    NodeDefinition,
    NodeMetadata,
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


# ---------- Defaults ----------


class TestMetadataDeclarationsDefaults:
    def test_library_metadata_declarations_defaults_to_empty(self) -> None:
        # Absence of any declaration is "no opinion" -- consumers (e.g.
        # ``requires_worker_process``) apply their own defaults rather than
        # the model materializing synthetic declarations.
        assert _make_library_metadata().declarations == []

    def test_node_metadata_declarations_defaults_to_empty(self) -> None:
        assert _make_node_metadata().declarations == []


# ---------- Discriminator behavior ----------


class TestDeclarationDiscriminator:
    def test_node_lifecycle_stage_round_trips(self) -> None:
        metadata = _make_node_metadata(declarations=[LifecycleStageNodeProperty(stage=LifecycleStage.BETA)])

        rebuilt = NodeMetadata.model_validate(metadata.model_dump())

        assert isinstance(rebuilt.declarations[0], LifecycleStageNodeProperty)
        assert rebuilt.declarations[0].stage is LifecycleStage.BETA

    def test_node_model_usage_round_trips(self) -> None:
        metadata = _make_node_metadata(declarations=[ModelUsageNodeProperty(model_ids=["claude_opus_byok"])])

        rebuilt = NodeMetadata.model_validate(metadata.model_dump())

        decl = rebuilt.declarations[0]
        assert isinstance(decl, ModelUsageNodeProperty)
        assert decl.model_ids == ["claude_opus_byok"]

    def test_node_arbitrary_python_execution_round_trips(self) -> None:
        metadata = _make_node_metadata(
            declarations=[ArbitraryPythonExecutionNodeProperty(executes_arbitrary_python=True)]
        )

        rebuilt = NodeMetadata.model_validate(metadata.model_dump())

        decl = rebuilt.declarations[0]
        assert isinstance(decl, ArbitraryPythonExecutionNodeProperty)
        assert decl.executes_arbitrary_python is True

    def test_library_lifecycle_stage_round_trips(self) -> None:
        metadata = _make_library_metadata(
            declarations=[LifecycleStageLibraryProperty(stage=LifecycleStage.STABLE)],
        )

        rebuilt = LibraryMetadata.model_validate(metadata.model_dump())

        assert isinstance(rebuilt.declarations[0], LifecycleStageLibraryProperty)
        assert rebuilt.declarations[0].stage is LifecycleStage.STABLE

    def test_unknown_node_type_discriminator_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            NodeMetadata.model_validate(
                {
                    "category": "Test",
                    "description": "t",
                    "display_name": "TestNode",
                    "declarations": [{"type": "not_a_real_type"}],
                }
            )

    def test_unknown_library_type_discriminator_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LibraryMetadata.model_validate(
                {
                    "author": "t",
                    "description": "t",
                    "library_version": "1.0.0",
                    "engine_version": "1.0.0",
                    "tags": [],
                    "declarations": [{"type": "not_a_real_type"}],
                }
            )


# ---------- LifecycleStage inheritance semantics ----------


class TestLifecycleStageSemantics:
    def test_node_stage_overrides_library_stage(self) -> None:
        node_metadata = _make_node_metadata(
            declarations=[LifecycleStageNodeProperty(stage=LifecycleStage.ALPHA)],
        )

        node_stage_decls = [d for d in node_metadata.declarations if isinstance(d, LifecycleStageNodeProperty)]
        assert len(node_stage_decls) == 1
        assert node_stage_decls[0].stage is LifecycleStage.ALPHA

    def test_library_stage_alone(self) -> None:
        lib_metadata = _make_library_metadata(
            declarations=[LifecycleStageLibraryProperty(stage=LifecycleStage.STABLE)],
        )

        lib_stage_decls = [d for d in lib_metadata.declarations if isinstance(d, LifecycleStageLibraryProperty)]
        assert len(lib_stage_decls) == 1
        assert lib_stage_decls[0].stage is LifecycleStage.STABLE


# ---------- Round-trip JSON serialization ----------


class TestRoundTripSerialization:
    def test_full_library_schema_serializes_and_deserializes(self) -> None:
        schema = LibrarySchema(
            name="Test Library",
            library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
            metadata=_make_library_metadata(
                declarations=[LifecycleStageLibraryProperty(stage=LifecycleStage.BETA)],
            ),
            categories=[{"Test": CategoryDefinition(title="Test", description="test", color="#000", icon="Folder")}],
            nodes=[
                NodeDefinition(
                    class_name="TestNode",
                    file_path="test_node.py",
                    metadata=_make_node_metadata(
                        declarations=[
                            LifecycleStageNodeProperty(stage=LifecycleStage.ALPHA),
                            ModelUsageNodeProperty(model_ids=["claude_opus_byok"]),
                        ],
                    ),
                ),
            ],
        )

        rebuilt = LibrarySchema.model_validate(json.loads(schema.model_dump_json()))

        assert rebuilt.metadata.declarations[0] == LifecycleStageLibraryProperty(stage=LifecycleStage.BETA)
        node_decls = rebuilt.nodes[0].metadata.declarations
        assert isinstance(node_decls[0], LifecycleStageNodeProperty)
        assert node_decls[0].stage is LifecycleStage.ALPHA
        assert isinstance(node_decls[1], ModelUsageNodeProperty)
        assert node_decls[1].model_ids == ["claude_opus_byok"]


# ---------- ArbitraryPythonExecutionNodeProperty ----------


class TestArbitraryPythonExecutionNodeProperty:
    def test_round_trips_each_value(self) -> None:
        for executes in (True, False):
            decl = ArbitraryPythonExecutionNodeProperty(executes_arbitrary_python=executes)

            rebuilt = ArbitraryPythonExecutionNodeProperty.model_validate(json.loads(decl.model_dump_json()))

            assert rebuilt.executes_arbitrary_python is executes

    def test_executes_arbitrary_python_is_required(self) -> None:
        # Single-value declarations require their value to be set explicitly;
        # absence of the entire declaration is the only meaningful "default."
        with pytest.raises(ValidationError):
            ArbitraryPythonExecutionNodeProperty()  # type: ignore[call-arg]

    def test_node_metadata_round_trips_declaration(self) -> None:
        metadata = _make_node_metadata(
            declarations=[ArbitraryPythonExecutionNodeProperty(executes_arbitrary_python=True)],
        )

        rebuilt = NodeMetadata.model_validate(metadata.model_dump())

        decl = rebuilt.declarations[0]
        assert isinstance(decl, ArbitraryPythonExecutionNodeProperty)
        assert decl.executes_arbitrary_python is True


# ---------- WorkerModeCompatibility ----------


class TestWorkerModeCompatibility:
    @pytest.mark.parametrize("compatibility", list(WorkerCompatibility))
    def test_round_trips_each_value(self, compatibility: WorkerCompatibility) -> None:
        decl = WorkerModeCompatibility(compatibility=compatibility)

        rebuilt = WorkerModeCompatibility.model_validate(json.loads(decl.model_dump_json()))

        assert rebuilt.compatibility is compatibility

    def test_compatibility_is_required(self) -> None:
        # Single-value declarations require their value to be set explicitly;
        # absence of the entire declaration is the only meaningful "default."
        with pytest.raises(ValidationError):
            WorkerModeCompatibility()  # type: ignore[call-arg]

    def test_rejects_unknown_compatibility_value(self) -> None:
        with pytest.raises(ValidationError):
            WorkerModeCompatibility.model_validate(
                {"type": "worker_mode_compatibility", "compatibility": "BOGUS"},
            )

    def test_library_metadata_round_trips_declaration(self) -> None:
        metadata = _make_library_metadata(
            declarations=[WorkerModeCompatibility(compatibility=WorkerCompatibility.INCOMPATIBLE)],
        )

        rebuilt = LibraryMetadata.model_validate(metadata.model_dump())

        decl = rebuilt.declarations[0]
        assert isinstance(decl, WorkerModeCompatibility)
        assert decl.compatibility is WorkerCompatibility.INCOMPATIBLE


# ---------- SuggestedWorkerMode ----------


class TestSuggestedWorkerMode:
    @pytest.mark.parametrize("mode", list(WorkerMode))
    def test_round_trips_each_value(self, mode: WorkerMode) -> None:
        decl = SuggestedWorkerMode(mode=mode)

        rebuilt = SuggestedWorkerMode.model_validate(json.loads(decl.model_dump_json()))

        assert rebuilt.mode is mode

    def test_mode_is_required(self) -> None:
        with pytest.raises(ValidationError):
            SuggestedWorkerMode()  # type: ignore[call-arg]

    def test_rejects_unknown_mode_value(self) -> None:
        with pytest.raises(ValidationError):
            SuggestedWorkerMode.model_validate({"type": "suggested_worker_mode", "mode": "BOGUS"})

    def test_library_metadata_round_trips_declaration(self) -> None:
        metadata = _make_library_metadata(
            declarations=[SuggestedWorkerMode(mode=WorkerMode.WORKER)],
        )

        rebuilt = LibraryMetadata.model_validate(metadata.model_dump())

        decl = rebuilt.declarations[0]
        assert isinstance(decl, SuggestedWorkerMode)
        assert decl.mode is WorkerMode.WORKER


# ---------- LibraryMetadata cross-declaration validator ----------


class TestLibraryMetadataWorkerValidation:
    def test_rejects_incompatible_with_suggested_worker_mode(self) -> None:
        # The two declarations live on the same metadata block and contradict
        # each other; the cross-axis check belongs on LibraryMetadata.
        with pytest.raises(ValidationError):
            _make_library_metadata(
                declarations=[
                    WorkerModeCompatibility(compatibility=WorkerCompatibility.INCOMPATIBLE),
                    SuggestedWorkerMode(mode=WorkerMode.WORKER),
                ],
            )

    def test_allows_incompatible_with_suggested_orchestrator(self) -> None:
        # INCOMPATIBLE + ORCHESTRATOR is consistent (redundant but legal):
        # the library can only run in the orchestrator, and the suggested
        # mode agrees.
        metadata = _make_library_metadata(
            declarations=[
                WorkerModeCompatibility(compatibility=WorkerCompatibility.INCOMPATIBLE),
                SuggestedWorkerMode(mode=WorkerMode.ORCHESTRATOR),
            ],
        )

        assert isinstance(metadata.declarations[0], WorkerModeCompatibility)
        assert isinstance(metadata.declarations[1], SuggestedWorkerMode)

    def test_allows_compatible_with_either_suggested_mode(self) -> None:
        for suggested in (WorkerMode.ORCHESTRATOR, WorkerMode.WORKER):
            metadata = _make_library_metadata(
                declarations=[
                    WorkerModeCompatibility(compatibility=WorkerCompatibility.COMPATIBLE),
                    SuggestedWorkerMode(mode=suggested),
                ],
            )
            assert isinstance(metadata.declarations[0], WorkerModeCompatibility)
            assert isinstance(metadata.declarations[1], SuggestedWorkerMode)

    def test_validator_runs_on_model_validate(self) -> None:
        # The validator must fire when LibraryMetadata is rebuilt from JSON,
        # not only when constructed directly. Pydantic's ``model_validate``
        # is the wire-format entry point; a regression that disables the
        # validator for that path silently lets bad manifests in.
        with pytest.raises(ValidationError):
            LibraryMetadata.model_validate(
                {
                    "author": "t",
                    "description": "t",
                    "library_version": "1.0.0",
                    "engine_version": "1.0.0",
                    "tags": [],
                    "declarations": [
                        {"type": "worker_mode_compatibility", "compatibility": "INCOMPATIBLE"},
                        {"type": "suggested_worker_mode", "mode": "WORKER"},
                    ],
                }
            )


# ---------- requires_worker_process free function ----------


class TestRequiresWorkerProcess:
    def test_no_declarations_returns_false(self) -> None:
        # Absence of both declarations is the orchestrator-default case.
        assert requires_worker_process([]) is False

    def test_compatible_without_suggested_mode_returns_false(self) -> None:
        # COMPATIBLE alone is "I can run as a worker"; without a suggested
        # mode the engine still defaults to orchestrator.
        decls = [WorkerModeCompatibility(compatibility=WorkerCompatibility.COMPATIBLE)]
        assert requires_worker_process(decls) is False

    def test_compatible_with_suggested_orchestrator_returns_false(self) -> None:
        decls = [
            WorkerModeCompatibility(compatibility=WorkerCompatibility.COMPATIBLE),
            SuggestedWorkerMode(mode=WorkerMode.ORCHESTRATOR),
        ]
        assert requires_worker_process(decls) is False

    def test_compatible_with_suggested_worker_returns_true(self) -> None:
        decls = [
            WorkerModeCompatibility(compatibility=WorkerCompatibility.COMPATIBLE),
            SuggestedWorkerMode(mode=WorkerMode.WORKER),
        ]
        assert requires_worker_process(decls) is True

    def test_incompatible_with_suggested_orchestrator_returns_false(self) -> None:
        decls = [
            WorkerModeCompatibility(compatibility=WorkerCompatibility.INCOMPATIBLE),
            SuggestedWorkerMode(mode=WorkerMode.ORCHESTRATOR),
        ]
        assert requires_worker_process(decls) is False

    def test_suggested_worker_alone_returns_true(self) -> None:
        # Absence of WorkerModeCompatibility is treated as COMPATIBLE per
        # the consumer-site default; a SuggestedWorkerMode of WORKER then
        # selects worker hosting.
        decls = [SuggestedWorkerMode(mode=WorkerMode.WORKER)]
        assert requires_worker_process(decls) is True


# ---------- Schema version ----------


class TestSchemaVersion:
    def test_latest_schema_version_is_0_10_0(self) -> None:
        assert LibrarySchema.LATEST_SCHEMA_VERSION == "0.10.0"


# ---------- Model catalog ----------


def _build_catalog() -> ModelCatalogLibraryProperty:
    """Build a catalog exercising multiple providers, family tags, and a model-less provider."""
    return ModelCatalogLibraryProperty(
        providers={
            "anthropic": ModelProvider(
                display_name="Anthropic",
                terms_url="https://example.com/anthropic/terms",
                models={
                    "claude_opus_byok": Model(
                        display_name="Claude Opus 4 (BYOK)",
                        family="Claude 4",
                        provider_model_id="claude-opus-4",
                        key_support=KeySupport.REQUIRES_CUSTOMER_KEY,
                        terms_url="https://example.com/anthropic/opus/terms",
                    ),
                    "claude_opus_griptape": Model(
                        display_name="Claude Opus 4 (Griptape Key)",
                        family="Claude 4",
                        provider_model_id="claude-opus-4",
                        key_support=KeySupport.REQUIRES_GRIPTAPE_KEY,
                    ),
                },
            ),
            "kling": ModelProvider(
                display_name="Kling",
                terms_url="https://example.com/kling/terms",
                models={
                    "kling_v2": Model(
                        display_name="Kling v2",
                        provider_model_id="kling-v2-master",
                        key_support=KeySupport.REQUIRES_GRIPTAPE_KEY,
                    ),
                },
            ),
            "ollama": ModelProvider(
                display_name="Ollama",
                key_support=KeySupport.NO_KEY_REQUIRED,
            ),
        },
    )


class TestModelCatalogRoundTrip:
    def test_full_catalog_round_trips(self) -> None:
        catalog = _build_catalog()

        rebuilt = ModelCatalogLibraryProperty.model_validate(json.loads(catalog.model_dump_json()))

        assert rebuilt == catalog

    def test_catalog_inside_library_metadata(self) -> None:
        metadata = _make_library_metadata(declarations=[_build_catalog()])

        rebuilt = LibraryMetadata.model_validate(metadata.model_dump())

        catalogs = [d for d in rebuilt.declarations if isinstance(d, ModelCatalogLibraryProperty)]
        assert len(catalogs) == 1
        assert "anthropic" in catalogs[0].providers
        assert "claude_opus_byok" in catalogs[0].providers["anthropic"].models

    def test_rejects_multiple_model_catalogs(self) -> None:
        with pytest.raises(ValidationError, match="at most one is allowed"):
            _make_library_metadata(declarations=[_build_catalog(), _build_catalog()])


class TestIterCatalogModels:
    def test_iterates_all_models_with_provider_context(self) -> None:
        catalog = _build_catalog()

        models = list(iter_catalog_models(catalog))

        ids = [r.model_id for r in models]
        assert sorted(ids) == ["claude_opus_byok", "claude_opus_griptape", "kling_v2"]
        kling = next(r for r in models if r.model_id == "kling_v2")
        assert kling.provider_id == "kling"
        anthropic = next(r for r in models if r.model_id == "claude_opus_byok")
        assert anthropic.provider_id == "anthropic"
        assert anthropic.model.family == "Claude 4"


class TestKeySupportEnum:
    def test_no_key_required_is_a_legal_value(self) -> None:
        # Models that run locally (e.g. Ollama-hosted) declare NO_KEY_REQUIRED
        # so admin tooling can distinguish them from Griptape-key / BYOK models.
        model = Model(display_name="Local Llama", key_support=KeySupport.NO_KEY_REQUIRED)

        rebuilt = Model.model_validate(json.loads(model.model_dump_json()))

        assert rebuilt.key_support is KeySupport.NO_KEY_REQUIRED


class TestCatalogNotesField:
    def test_provider_and_model_each_carry_notes(self) -> None:
        catalog = ModelCatalogLibraryProperty(
            providers={
                "p": ModelProvider(
                    display_name="Provider",
                    notes="provider-level note",
                    models={
                        "m": Model(
                            display_name="Model",
                            key_support=KeySupport.REQUIRES_CUSTOMER_KEY,
                            notes="model-level note",
                        ),
                    },
                ),
            },
        )

        rebuilt = ModelCatalogLibraryProperty.model_validate(json.loads(catalog.model_dump_json()))

        provider = rebuilt.providers["p"]
        assert provider.notes == "provider-level note"
        assert provider.models["m"].notes == "model-level note"

    def test_notes_default_to_none(self) -> None:
        model = Model(display_name="X", key_support=KeySupport.REQUIRES_GRIPTAPE_KEY)

        assert model.notes is None


class TestModelFamilyTag:
    def test_family_defaults_to_none(self) -> None:
        model = Model(display_name="X", key_support=KeySupport.REQUIRES_GRIPTAPE_KEY)

        assert model.family is None

    def test_family_round_trips(self) -> None:
        model = Model(display_name="X", family="GPT-4", key_support=KeySupport.REQUIRES_GRIPTAPE_KEY)

        rebuilt = Model.model_validate(json.loads(model.model_dump_json()))

        assert rebuilt.family == "GPT-4"


class TestProviderLevelKeySupport:
    def test_provider_key_support_is_optional(self) -> None:
        # Most providers omit key_support at the provider level (models carry it).
        provider = ModelProvider(display_name="P")

        assert provider.key_support is None

    def test_provider_can_declare_no_key_required(self) -> None:
        # The Ollama-style case: provider declares NO_KEY_REQUIRED with no models.
        provider = ModelProvider(display_name="Ollama (local)", key_support=KeySupport.NO_KEY_REQUIRED)

        rebuilt = ModelProvider.model_validate(json.loads(provider.model_dump_json()))

        assert rebuilt.key_support is KeySupport.NO_KEY_REQUIRED


class TestModelUsageRoundTrip:
    def test_round_trip(self) -> None:
        decl = ModelUsageNodeProperty(model_ids=["claude_opus_byok", "kling_v2"])

        rebuilt = ModelUsageNodeProperty.model_validate(json.loads(decl.model_dump_json()))

        assert rebuilt == decl


class TestModelProviderUsageRoundTrip:
    def test_round_trip(self) -> None:
        decl = ModelProviderUsageNodeProperty(provider_ids=["anthropic", "openai"])

        rebuilt = ModelProviderUsageNodeProperty.model_validate(json.loads(decl.model_dump_json()))

        assert rebuilt == decl

    def test_inside_node_metadata(self) -> None:
        metadata = _make_node_metadata(
            declarations=[ModelProviderUsageNodeProperty(provider_ids=["anthropic"])],
        )

        rebuilt = NodeMetadata.model_validate(metadata.model_dump())

        decl = rebuilt.declarations[0]
        assert isinstance(decl, ModelProviderUsageNodeProperty)
        assert decl.provider_ids == ["anthropic"]


class TestResolveNodeModels:
    def test_model_usage_resolves_in_declaration_order(self) -> None:
        catalog = _build_catalog()
        decls = [ModelUsageNodeProperty(model_ids=["kling_v2", "claude_opus_byok"])]

        resolved = resolve_node_models(catalog, decls)

        assert [r.model_id for r in resolved] == ["kling_v2", "claude_opus_byok"]

    def test_provider_usage_resolves_all_provider_models_in_catalog_order(self) -> None:
        catalog = _build_catalog()
        decls = [ModelProviderUsageNodeProperty(provider_ids=["anthropic"])]

        resolved = resolve_node_models(catalog, decls)

        assert [r.model_id for r in resolved] == ["claude_opus_byok", "claude_opus_griptape"]
        assert {r.provider_id for r in resolved} == {"anthropic"}

    def test_provider_usage_for_model_less_provider_is_empty(self) -> None:
        # Ollama declares no models; a provider reference resolves to nothing,
        # leaving the node to enumerate its models dynamically at runtime.
        catalog = _build_catalog()

        resolved = resolve_node_models(catalog, [ModelProviderUsageNodeProperty(provider_ids=["ollama"])])

        assert resolved == []

    def test_combination_dedups_keeping_first_occurrence(self) -> None:
        # A model named directly and again via its provider appears once, at the
        # position of its first mention.
        catalog = _build_catalog()
        decls = [
            ModelUsageNodeProperty(model_ids=["claude_opus_byok"]),
            ModelProviderUsageNodeProperty(provider_ids=["anthropic"]),
        ]

        resolved = resolve_node_models(catalog, decls)

        assert [r.model_id for r in resolved] == ["claude_opus_byok", "claude_opus_griptape"]

    def test_unresolved_references_are_skipped(self) -> None:
        catalog = _build_catalog()
        decls = [
            ModelUsageNodeProperty(model_ids=["does_not_exist"]),
            ModelProviderUsageNodeProperty(provider_ids=["also_missing"]),
        ]

        assert resolve_node_models(catalog, decls) == []

    def test_non_model_declarations_are_ignored(self) -> None:
        catalog = _build_catalog()
        decls = [LifecycleStageNodeProperty(stage=LifecycleStage.BETA)]

        assert resolve_node_models(catalog, decls) == []

    def test_carries_provider_and_model_context(self) -> None:
        catalog = _build_catalog()

        resolved = resolve_node_models(catalog, [ModelUsageNodeProperty(model_ids=["claude_opus_byok"])])

        (entry,) = resolved
        assert entry.provider_id == "anthropic"
        assert entry.model.provider_model_id == "claude-opus-4"
        assert entry.model.family == "Claude 4"
        assert entry.provider.display_name == "Anthropic"
