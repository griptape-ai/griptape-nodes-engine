"""Tests for the policy layer shared by the two model-dropdown components.

`ModelAccessComponent` (static, author-enumerated choices) and `HuggingFaceModelParameter`
(choices from a local cache scan) own their `Parameter` differently and do not compose, but they
must never answer "is this model permitted?" differently. These tests pin the shared contract, and
in particular the one axis on which the two are ALLOWED to differ: `refuse_unrecognized`.
"""

import logging
from collections.abc import Callable, Generator
from dataclasses import FrozenInstanceError
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from griptape_nodes.exe_types.param_components.model_policy import (
    DEFERRED_SNAPSHOT,
    ModelPolicySnapshot,
    node_access_request,
    query_model_policy,
)
from griptape_nodes.node_library.library_declarations import (
    KeySupport,
    Model,
    ModelCatalogLibraryProperty,
    ModelProvider,
    ModelUsageNodeProperty,
)
from griptape_nodes.node_library.library_registry import (
    LibraryMetadata,
    LibraryRegistry,
    LibrarySchema,
    NodeMetadata,
)
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.access_events import (
    ModelAccessVerdict,
    QueryModelAccessForNodeResultFailure,
    QueryModelAccessForNodeResultSuccess,
)
from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure
from tests.unit.exe_types.mocks import MockNode
from tests.unit.exe_types.param_components.probe_scope import constructing_under_probe


class SomeNode(MockNode):
    """The node under query. Its class name is the node type the policy layer asks about."""


def _node(
    result: object | None = None,
    *,
    side_effect: Callable[[object], object] | None = None,
    library_name: str | None = None,
    node_type: str | None = None,
) -> SomeNode:
    """A stand-in node whose engine answers ``handle_request`` with a canned result.

    ``query_model_policy`` reads everything it needs off the node -- the engine to ask, and the
    registering library and node type in ``metadata`` -- so these tests hand it one rather than
    patching a process-wide accessor. Omitting ``library_name`` or ``node_type`` models a node built
    outside the library path, which recorded neither.
    """
    engine = MagicMock()
    if side_effect is not None:
        engine.handle_request.side_effect = side_effect
    else:
        engine.handle_request.return_value = result
    metadata: dict[str, str] = {}
    if library_name is not None:
        metadata["library"] = library_name
    if node_type is not None:
        metadata["node_type"] = node_type
    return SomeNode(name="some_node", metadata=metadata, engine=engine)


def _engine_of(node: SomeNode) -> MagicMock:
    """The mock behind ``node.engine``, for asserting on the requests it did or did not receive.

    ``BaseNode.engine`` is typed ``Engine``, so reaching the mock's call record through it is a type
    error even though the object IS the mock ``_node`` installed. The cast lives here so the tests
    that need the call record still build their node with ``_node``.
    """
    return cast("MagicMock", node.engine)


DENIED = "owner/denied"
ALLOWED = "owner/allowed"
UNKNOWN = "owner/never-declared"

_DENIAL = CheckpointDenial(failures=(CheckpointFailure(detail="Forbidden by your license."),))


def _success(verdicts: list[ModelAccessVerdict]) -> QueryModelAccessForNodeResultSuccess:
    return QueryModelAccessForNodeResultSuccess(verdicts=verdicts, result_details="ok")


class TestNodeAccessRequest:
    """Naming the node type is only half a query; the other half is which library it came from."""

    def test_it_carries_both_the_node_type_and_the_library(self) -> None:
        request = node_access_request(_node(library_name="library-standard"))
        assert request.node_type == "SomeNode"
        assert request.specific_library_name == "library-standard"
        # No narrowing, so the engine derives candidates from the node's declarations.
        assert request.candidate_model_ids is None

    def test_a_narrowed_candidate_list_is_forwarded(self) -> None:
        """The live per-value re-ask a component makes at run time narrows to one handle's ids."""
        request = node_access_request(_node(library_name="library-standard"), ["md_a", "md_b"])
        assert request.candidate_model_ids == ["md_a", "md_b"]

    def test_the_node_type_is_the_name_the_library_registered(self) -> None:
        """Not ``__name__``: the registry keys a node type by the name its library JSON declared.

        ``register_lazy_node_type`` never imports the class to compare the two, so a module that
        aliases its class registers under one name and reports the other. The engine resolves by the
        registry key, which is what ``Library.create_node`` records in ``metadata["node_type"]``.
        """
        request = node_access_request(_node(library_name="library-standard", node_type="AliasKey"))
        assert request.node_type == "AliasKey"

    def test_a_node_that_recorded_no_type_falls_back_to_its_class_name(self) -> None:
        """The probe / fixture case: nothing registered it, so the class name is all there is."""
        assert node_access_request(_node()).node_type == "SomeNode"

    def test_a_node_built_outside_the_library_path_names_no_library(self) -> None:
        """A transient probe or a test fixture has no ``library`` metadata.

        Lookup by name alone is the right fallback there: it resolves correctly whenever exactly
        one library declares the type, which is every case except a collision.
        """
        assert node_access_request(_node()).specific_library_name is None

    def test_the_policy_query_carries_it(self) -> None:
        """Pinned on the request the bus actually saw, not just on the builder in isolation."""
        node = _node(_success([]), library_name="library-standard")
        query_model_policy(node)
        request = _engine_of(node).handle_request.call_args.args[0]
        assert request.node_type == "SomeNode"
        assert request.specific_library_name == "library-standard"


def _register_node_type(library_name: str, model_id: str, node_class: type[MockNode], *, registered_as: str) -> None:
    """Register ``node_class`` in a fresh ``library_name`` over a one-model catalog.

    ``registered_as`` is the registry key -- the class name the library's JSON declares -- and is
    passed separately from ``node_class`` because the two need not agree: ``register_lazy_node_type``
    stores the declared name without importing the class to compare it against ``__name__``.
    Registering lazily is also how a real library, loaded from its JSON, arrives.
    """
    catalog = ModelCatalogLibraryProperty(
        providers={
            "bfl": ModelProvider(
                display_name="Black Forest Labs",
                models={
                    model_id: Model(
                        display_name="FLUX.2",
                        provider_model_id=f"{model_id}-handle",
                        key_support=KeySupport.REQUIRES_GRIPTAPE_KEY,
                    )
                },
            )
        }
    )
    schema = LibrarySchema(
        name=library_name,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="t",
            description="d",
            library_version="1.0.0",
            engine_version="1.0.0",
            tags=[],
            declarations=[catalog],
        ),
        categories=[],
        nodes=[],
    )
    library = LibraryRegistry.generate_new_library(library_data=schema)
    library.register_lazy_node_type(
        registered_as,
        NodeMetadata(
            category="t",
            description="d",
            display_name="FLUX.2 Image Generation",
            declarations=[ModelUsageNodeProperty(model_ids=[model_id])],
        ),
        lambda: node_class,
    )


class TestALibraryThatDeclaresAnAliasedClass:
    """A node type is registered under the name its library declared, not the class's ``__name__``.

    `register_lazy_node_type` stores that declared name without importing the class to compare, so a
    module that aliases its class (`AliasKey = RealClass`) registers under one name and reports the
    other. `Library.create_node` records the declared name in `metadata["node_type"]` and the engine
    resolves by it, so a query naming `__name__` finds no such type and fails closed on a node whose
    library resolves perfectly well. `get_declared_models` -- which fills the same dropdown's choices
    -- reads the declared name, so querying policy by `__name__` would gate choices it cannot judge.
    """

    _LIBRARY = "library-standard"
    _REGISTERED_AS = "AliasKey"
    _MODEL_ID = "md_aliased_flux"

    @pytest.fixture(autouse=True)
    def _clean_registry(self) -> Generator[None, None, None]:
        LibraryRegistry._clear()
        yield
        LibraryRegistry._clear()

    def test_the_query_resolves_through_the_registered_name(self, engine: Engine) -> None:
        node_class = type("RealClassName", (MockNode,), {})
        _register_node_type(self._LIBRARY, self._MODEL_ID, node_class, registered_as=self._REGISTERED_AS)

        node = node_class(
            name="flux",
            metadata={"library": self._LIBRARY, "node_type": self._REGISTERED_AS},
            engine=engine,
        )
        snapshot = query_model_policy(node)

        assert snapshot.failure_detail is None
        assert snapshot.catalog_ids_for(f"{self._MODEL_ID}-handle") == (self._MODEL_ID,)

    def test_the_fail_closed_warning_names_the_type_the_engine_was_asked_about(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The warning must not name a type nobody queried.

        It is what a library author reads when every model on a node locks, and `RealClassName` sends
        them after a registration that was never the problem.

        No registration here: the engine is stubbed to fail, because what is under test is which name
        the log carries, not whether the lookup could have succeeded.
        """
        engine = MagicMock()
        engine.handle_request.return_value = QueryModelAccessForNodeResultFailure(result_details="not registered")
        node_class = type("RealClassName", (MockNode,), {})
        node = node_class(
            name="flux",
            metadata={"library": self._LIBRARY, "node_type": self._REGISTERED_AS},
            engine=engine,
        )

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            snapshot = query_model_policy(node)

        assert snapshot.failure_detail is not None
        assert self._REGISTERED_AS in caplog.text
        assert "RealClassName" not in caplog.text


class TestTwoLibrariesRegisteringOneNodeType:
    """A node class name two installed libraries share must still resolve to one library.

    Installing the standard library beside an extension library that reuses a node class name is
    supported -- `Flux2ImageGeneration` ships in both the standard and the Black Forest Labs
    library. The engine cannot resolve that name to a library on its own, so a query naming only
    the type resolved to none of them, failed closed, and locked every model on the node. To an
    artist that reads as a licensing problem, when in fact the access check never ran.
    """

    _STANDARD = "library-standard"
    _EXTENSION = "library-extension"
    _NODE_TYPE = "Flux2ImageGeneration"

    @pytest.fixture(autouse=True)
    def _clean_registry(self) -> Generator[None, None, None]:
        LibraryRegistry._clear()
        yield
        LibraryRegistry._clear()

    def test_the_nodes_own_library_answers(self, engine: Engine) -> None:
        standard_node_class = self._register(self._STANDARD, "md_standard_flux")
        self._register(self._EXTENSION, "md_extension_flux")

        node = standard_node_class(
            name="flux",
            metadata={"library": self._STANDARD, "node_type": self._NODE_TYPE},
            engine=engine,
        )
        snapshot = query_model_policy(node)

        assert snapshot.failure_detail is None
        # The standard library's catalog, not the extension's, and nothing denied.
        assert snapshot.catalog_ids_for("md_standard_flux-handle") == ("md_standard_flux",)
        assert snapshot.catalog_ids_for("md_extension_flux-handle") == ()
        assert snapshot.denial_for("md_standard_flux-handle") is None

    def test_a_node_with_no_library_metadata_still_fails_closed(self, engine: Engine) -> None:
        """The fallback's limit, stated: by-name lookup cannot pick between two libraries.

        This is the reported failure, and it remains the answer for a node that never recorded
        which library built it. Failing closed is correct here -- the access check genuinely did
        not run, and an unresolvable node must not open the gate.
        """
        standard_node_class = self._register(self._STANDARD, "md_standard_flux")
        self._register(self._EXTENSION, "md_extension_flux")

        node = standard_node_class(name="flux", metadata={}, engine=engine)

        assert query_model_policy(node).failure_detail is not None

    def _register(self, library_name: str, model_id: str) -> type[MockNode]:
        """Register ``_NODE_TYPE`` in ``library_name`` over a one-model catalog; return its class."""
        # What collides is the declared name both libraries register under, which is what
        # `registered_as` carries on each call. The two classes are distinct objects that also share
        # a `__name__`; that part is not load-bearing here, and is kept only because it is what the
        # real libraries look like -- each ships its own `class Flux2ImageGeneration`.
        node_class = type(self._NODE_TYPE, (MockNode,), {})
        _register_node_type(library_name, model_id, node_class, registered_as=self._NODE_TYPE)
        return node_class


class TestQueryModelPolicy:
    def test_builds_both_tables_from_one_query(self) -> None:
        verdicts = [
            ModelAccessVerdict(model_id="md_denied", provider_model_id=DENIED, denial=_DENIAL),
            ModelAccessVerdict(model_id="md_allowed", provider_model_id=ALLOWED, denial=None),
        ]
        snapshot = query_model_policy(_node(_success(verdicts)))
        assert snapshot.denial_by_provider_id == {DENIED: _DENIAL}
        assert snapshot.catalog_ids_by_provider_id == {DENIED: ("md_denied",), ALLOWED: ("md_allowed",)}
        assert snapshot.failure_detail is None
        assert snapshot.has_unmatchable_entries is False

    def test_fail_closed_records_a_failure_detail(self) -> None:
        snapshot = query_model_policy(_node(QueryModelAccessForNodeResultFailure(result_details="not found")))
        assert snapshot.failure_detail is not None
        assert snapshot.denial_for(ALLOWED) is not None

    def test_the_failure_detail_reads_for_an_artist(self, caplog: pytest.LogCaptureFixture) -> None:
        """This string reaches a badge and a run error, so it must not name manifest internals.

        The node type, the engine's reason, and the "declare a model_usage block" instruction are
        for whoever maintains the library, and belong in the log. An artist cannot act on any of
        them, and a registration problem worded as a licensing one sends them looking in the wrong
        place entirely.
        """
        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            snapshot = query_model_policy(_node(QueryModelAccessForNodeResultFailure(result_details="not registered")))

        detail = snapshot.failure_detail
        assert detail is not None
        for jargon in ("model_usage", "manifest", "griptape_nodes_library.json", "SomeNode", "not registered"):
            assert jargon not in detail
        assert "Contact whoever maintains this node library." in detail
        # The author-facing diagnostic is not lost -- it moved to the log.
        assert "model_usage block" in caplog.text
        assert "SomeNode" in caplog.text
        assert "not registered" in caplog.text

    def test_fail_open_records_nothing(self) -> None:
        """Auto-detect uses this: an unresolvable node means "has not adopted declarations"."""
        snapshot = query_model_policy(
            _node(QueryModelAccessForNodeResultFailure(result_details="not found")), fail_closed=False
        )
        assert snapshot.failure_detail is None
        assert snapshot.declares_models is False

    def test_a_model_without_a_provider_handle_is_declared_but_unmatchable(self) -> None:
        """`provider_model_id` is optional, and absence is NOT "unresolved"."""
        verdicts = [ModelAccessVerdict(model_id="md_no_handle", provider_model_id=None, denial=None)]
        snapshot = query_model_policy(_node(_success(verdicts)))
        assert snapshot.has_unmatchable_entries is True
        assert snapshot.catalog_ids_by_provider_id == {}
        # Still counts as declaring models -- otherwise enforcement would silently switch off.
        assert snapshot.declares_models is True


class TestDenialFor:
    def test_an_explicit_denial_is_returned(self) -> None:
        snapshot = ModelPolicySnapshot(
            denial_by_provider_id={DENIED: _DENIAL}, catalog_ids_by_provider_id={DENIED: ("x",)}
        )
        assert snapshot.denial_for(DENIED) is _DENIAL

    def test_a_permitted_model_is_allowed(self) -> None:
        snapshot = ModelPolicySnapshot(catalog_ids_by_provider_id={ALLOWED: ("x",)})
        assert snapshot.denial_for(ALLOWED) is None

    def test_none_is_never_denied(self) -> None:
        """A placeholder row or a connected driver object is not a model.

        Pinned against a snapshot that carries every whole-parameter refusal there is, since those
        are exactly the paths that would deny a value which is not a model at all -- badging the
        "no models downloaded" placeholder as unlicensed, and replacing the "download this model"
        message with a license error.
        """
        snapshot = ModelPolicySnapshot(
            failure_detail="could not evaluate",
            unmatchable_denials=("md_flux_dev",),
            has_unmatchable_entries=True,
            catalog_ids_by_provider_id={ALLOWED: ("x",)},
        )
        assert snapshot.denial_for(None, refuse_unrecognized=True) is None
        assert snapshot.denial_for(None) is None

    def test_a_failure_detail_denies_everything(self) -> None:
        snapshot = ModelPolicySnapshot(failure_detail="could not evaluate")
        assert snapshot.denial_for(ALLOWED) is not None

    def test_unrecognized_is_allowed_by_default(self) -> None:
        """The static-dropdown stance: an unknown id was vetted at authoring time."""
        snapshot = ModelPolicySnapshot(catalog_ids_by_provider_id={ALLOWED: ("x",)})
        assert snapshot.denial_for(UNKNOWN) is None

    def test_unrecognized_is_refused_when_asked(self) -> None:
        """The cache-scan stance: an unknown repo could be anything the artist pulled down."""
        snapshot = ModelPolicySnapshot(catalog_ids_by_provider_id={ALLOWED: ("x",)})
        denial = snapshot.denial_for(UNKNOWN, refuse_unrecognized=True)
        assert denial is not None
        # Artist-facing wording: names the model, no manifest-editing instructions.
        assert UNKNOWN in denial.reason()
        assert "provider_model_id" not in denial.reason()

    def test_unrecognized_is_allowed_when_the_catalog_view_is_incomplete(self) -> None:
        """An unmatchable entry means absence proves nothing, so the refusal is suppressed.

        Without this, a library author who declared a model with only the two required fields would
        have it blocked, with an error telling them to declare what they already declared.
        """
        snapshot = ModelPolicySnapshot(catalog_ids_by_provider_id={ALLOWED: ("x",)}, has_unmatchable_entries=True)
        assert snapshot.denial_for(UNKNOWN, refuse_unrecognized=True) is None

    def test_explicit_denials_survive_an_incomplete_view(self) -> None:
        """Suppressing undeclared-refusal must not suppress real policy denials."""
        snapshot = ModelPolicySnapshot(
            denial_by_provider_id={DENIED: _DENIAL},
            catalog_ids_by_provider_id={DENIED: ("x",)},
            has_unmatchable_entries=True,
        )
        assert snapshot.denial_for(DENIED, refuse_unrecognized=True) is _DENIAL


class TestAnUnattributableDenialIsNotDropped:
    """A denial policy handed us must be honored even when no row can carry it.

    `provider_model_id` is optional on a catalog `Model`, so an author can declare a model with
    only the two required fields. If policy DENIES such an entry, there is no handle to match it to
    a dropdown value -- and the same absence switches off the undeclared backstop. Dropping the
    denial would mean both enforcement layers fail off together and the forbidden weights run.
    """

    def test_the_whole_parameter_is_refused(self) -> None:
        snapshot = query_model_policy(_node(_success([ModelAccessVerdict("md_flux_dev", None, _DENIAL)])))
        assert snapshot.unmatchable_denials == ("md_flux_dev",)
        denial = snapshot.denial_for(ALLOWED, refuse_unrecognized=True)
        assert denial is not None
        # The escalation must reach the artist as an effect, not as a manifest instruction; the
        # catalog id and the fix belong in the log warning instead.
        assert "provider_model_id" not in denial.reason()
        assert "md_flux_dev" not in denial.reason()

    def test_a_permitted_handleless_entry_does_not_refuse_anything(self) -> None:
        """Only a DENIED unmatchable entry escalates; a permitted one is merely unmatchable."""
        snapshot = query_model_policy(_node(_success([ModelAccessVerdict("md_no_handle", None, None)])))
        assert snapshot.unmatchable_denials == ()
        assert snapshot.has_unmatchable_entries is True
        assert snapshot.denial_for(ALLOWED, refuse_unrecognized=True) is None

    def test_it_applies_even_with_refuse_unrecognized_off(self) -> None:
        """A static dropdown must not run a model policy explicitly forbade either."""
        snapshot = query_model_policy(_node(_success([ModelAccessVerdict("md_flux_dev", None, _DENIAL)])))
        assert snapshot.denial_for(ALLOWED) is not None


class TestASharedProviderModelIdIsNotLastWriteWins:
    """Two catalog entries may declare the same ``provider_model_id`` with different key support.

    `Model`'s contract sanctions this (a BYOK entry beside a hosted-key entry). If the tables were
    last-write-wins, a permitted twin arriving after a denied one would erase the denial and the
    forbidden entry would run.
    """

    def test_a_denial_survives_a_permitted_twin(self) -> None:
        shared = "black-forest-labs/FLUX.1-dev"
        verdicts = [
            ModelAccessVerdict(model_id="md_flux_byok", provider_model_id=shared, denial=_DENIAL),
            ModelAccessVerdict(model_id="md_flux_gtc", provider_model_id=shared, denial=None),
        ]
        snapshot = query_model_policy(_node(_success(verdicts)))
        assert snapshot.denial_for(shared) is _DENIAL

    def test_every_catalog_id_behind_a_handle_is_retained(self) -> None:
        """Callers that re-ask policy live must ask about all of them, not whichever was last."""
        shared = "black-forest-labs/FLUX.1-dev"
        verdicts = [
            ModelAccessVerdict(model_id="md_flux_byok", provider_model_id=shared, denial=None),
            ModelAccessVerdict(model_id="md_flux_gtc", provider_model_id=shared, denial=None),
        ]
        snapshot = query_model_policy(_node(_success(verdicts)))
        assert snapshot.catalog_ids_for(shared) == ("md_flux_byok", "md_flux_gtc")

    def test_an_unknown_handle_has_no_catalog_ids(self) -> None:
        assert ModelPolicySnapshot().catalog_ids_for(UNKNOWN) == ()


class TestBothComponentsAgreeOnAnUnattributableDenial:
    """The decoration path and the run path must reach the same verdict.

    An unattributable denial is a decision about the WHOLE parameter, so a live per-id re-query
    cannot answer it -- the entry with no `provider_model_id` is by definition absent from any
    candidate list. A run path that skipped the snapshot check greyed out every row, told the artist
    "running this node will fail", and then ran the node anyway.
    """

    def test_the_static_component_run_path_honors_it(self) -> None:
        from griptape_nodes.exe_types.core_types import Parameter
        from griptape_nodes.exe_types.param_components.model_access_component import ModelAccessComponent

        verdicts = [
            ModelAccessVerdict(model_id="md_unattributable", provider_model_id=None, denial=_DENIAL),
            ModelAccessVerdict(model_id="md_ok", provider_model_id="alpha", denial=None),
        ]

        def handle(request: object) -> object:
            candidates = getattr(request, "candidate_model_ids", None)
            chosen = [v for v in verdicts if v.model_id in candidates] if candidates else verdicts
            return _success(chosen)

        node = MockNode()
        parameter = Parameter(name="model", type="str", default_value="alpha", tooltip="m")
        node.add_parameter(parameter)
        with patch("griptape_nodes.retained_mode.engine.Engine.handle_request", side_effect=handle):
            component = ModelAccessComponent(
                node=node, parameter=parameter, model_choices=["alpha"], default_model="alpha"
            )
            # Decoration and the run gate must not disagree.
            assert component._cached_denial("alpha") is not None
            assert component.query_for_denial("alpha") is not None
            with pytest.raises(RuntimeError):
                component.raise_if_denied("alpha")


class TestConstructionDeferral:
    """The query is skipped only where issuing it would trip reentrant-bus-in-init.

    That rule fires for a bus request made while a node __init__ is on the stack AND a
    strict-mode scope is open -- the worker's schema probe (where the violation drops the
    class from the worker schema) or node execution. `query_model_policy` must not touch the
    bus there, and the deferred snapshot it returns instead must deny nothing: no query was
    made, so there is no verdict (and no failure) to enforce.

    Construction with no scope open -- an editor drop, a workflow load, any single-process
    engine -- must still query, because that is where the dropdown's denial rows and badge
    come from. Deferring there is what made decoration wait for the first run.
    """

    def test_no_bus_request_while_constructing_under_a_probe_scope(self) -> None:
        node = _node()
        with constructing_under_probe():
            snapshot = query_model_policy(node)
        _engine_of(node).handle_request.assert_not_called()
        assert snapshot.deferred is True

    def test_construction_outside_a_strict_mode_scope_queries_normally(self) -> None:
        """The regression that motivated narrowing the condition.

        An editor drop constructs the node with no scope open, so nothing would observe the
        violation and no probe exists to deadlock. Skipping the query there stripped denial
        rows and the badge until the node ran.
        """
        verdicts = [ModelAccessVerdict(model_id="md_denied", provider_model_id=DENIED, denial=_DENIAL)]
        with LibraryRegistry.constructing_node():
            snapshot = query_model_policy(_node(_success(verdicts)))
        assert snapshot.deferred is False
        assert snapshot.denial_for(DENIED) is _DENIAL

    def test_a_deferred_snapshot_denies_nothing_even_when_asked_to_refuse_unrecognized(self) -> None:
        """The regression this exists for: a naive empty snapshot DOES refuse unrecognized ids.

        Skipping the query without the `deferred` marker would badge every choice on a gated
        dropdown "not permitted" at construction. Pin the contrast explicitly.
        """
        assert ModelPolicySnapshot().denial_for(UNKNOWN, refuse_unrecognized=True) is not None
        assert DEFERRED_SNAPSHOT.denial_for(UNKNOWN, refuse_unrecognized=True) is None
        assert DEFERRED_SNAPSHOT.denial_for(UNKNOWN) is None

    def test_a_deferred_snapshot_is_not_fail_closed(self) -> None:
        """Deferral is "not yet asked", not "could not answer" -- it must not read as a failure."""
        assert DEFERRED_SNAPSHOT.failure_detail is None
        assert DEFERRED_SNAPSHOT.declares_models is False

    def test_the_query_goes_through_once_construction_ends(self) -> None:
        verdicts = [ModelAccessVerdict(model_id="md_denied", provider_model_id=DENIED, denial=_DENIAL)]
        node = _node(_success(verdicts))
        with constructing_under_probe():
            assert query_model_policy(node).deferred is True
        snapshot = query_model_policy(node)
        assert snapshot.deferred is False
        assert snapshot.denial_for(DENIED) is _DENIAL


class TestSnapshotIsImmutable:
    def test_frozen(self) -> None:
        """Callers replace the snapshot wholesale, so the tables cannot drift apart."""
        snapshot = ModelPolicySnapshot()
        with pytest.raises(FrozenInstanceError):
            snapshot.failure_detail = "mutated"  # type: ignore[misc]
