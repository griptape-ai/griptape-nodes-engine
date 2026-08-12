"""Tests for the policy layer shared by the two model-dropdown components.

`ModelAccessComponent` (static, author-enumerated choices) and `HuggingFaceModelParameter`
(choices from a local cache scan) own their `Parameter` differently and do not compose, but they
must never answer "is this model permitted?" differently. These tests pin the shared contract, and
in particular the one axis on which the two are ALLOWED to differ: `refuse_unrecognized`.
"""

import logging
from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock, patch

import pytest

from griptape_nodes.exe_types.param_components.model_policy import (
    DEFERRED_SNAPSHOT,
    ModelPolicySnapshot,
    query_model_policy,
)
from griptape_nodes.node_library.library_registry import LibraryRegistry
from griptape_nodes.retained_mode.events.access_events import (
    ModelAccessVerdict,
    QueryModelAccessForNodeResultFailure,
    QueryModelAccessForNodeResultSuccess,
)
from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure

_HANDLE = "griptape_nodes.exe_types.param_components.model_policy.GriptapeNodes.handle_request"

DENIED = "owner/denied"
ALLOWED = "owner/allowed"
UNKNOWN = "owner/never-declared"

_DENIAL = CheckpointDenial(failures=(CheckpointFailure(detail="Forbidden by your license."),))


def _success(verdicts: list[ModelAccessVerdict]) -> QueryModelAccessForNodeResultSuccess:
    return QueryModelAccessForNodeResultSuccess(verdicts=verdicts, result_details="ok")


class TestQueryModelPolicy:
    def test_builds_both_tables_from_one_query(self) -> None:
        verdicts = [
            ModelAccessVerdict(model_id="md_denied", provider_model_id=DENIED, denial=_DENIAL),
            ModelAccessVerdict(model_id="md_allowed", provider_model_id=ALLOWED, denial=None),
        ]
        with patch(_HANDLE, return_value=_success(verdicts)):
            snapshot = query_model_policy("SomeNode")
        assert snapshot.denial_by_provider_id == {DENIED: _DENIAL}
        assert snapshot.catalog_ids_by_provider_id == {DENIED: ("md_denied",), ALLOWED: ("md_allowed",)}
        assert snapshot.failure_detail is None
        assert snapshot.has_unmatchable_entries is False

    def test_fail_closed_records_a_failure_detail(self) -> None:
        with patch(_HANDLE, return_value=QueryModelAccessForNodeResultFailure(result_details="not found")):
            snapshot = query_model_policy("SomeNode")
        assert snapshot.failure_detail is not None
        assert snapshot.denial_for(ALLOWED) is not None

    def test_the_failure_detail_reads_for_an_artist(self, caplog: pytest.LogCaptureFixture) -> None:
        """This string reaches a badge and a run error, so it must not name manifest internals.

        The node type, the engine's reason, and the "declare a model_usage block" instruction are
        for whoever maintains the library, and belong in the log. An artist cannot act on any of
        them, and a registration problem worded as a licensing one sends them looking in the wrong
        place entirely.
        """
        with (
            caplog.at_level(logging.WARNING, logger="griptape_nodes"),
            patch(_HANDLE, return_value=QueryModelAccessForNodeResultFailure(result_details="not registered")),
        ):
            snapshot = query_model_policy("SomeNode")

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
        with patch(_HANDLE, return_value=QueryModelAccessForNodeResultFailure(result_details="not found")):
            snapshot = query_model_policy("SomeNode", fail_closed=False)
        assert snapshot.failure_detail is None
        assert snapshot.declares_models is False

    def test_a_model_without_a_provider_handle_is_declared_but_unmatchable(self) -> None:
        """`provider_model_id` is optional, and absence is NOT "unresolved"."""
        verdicts = [ModelAccessVerdict(model_id="md_no_handle", provider_model_id=None, denial=None)]
        with patch(_HANDLE, return_value=_success(verdicts)):
            snapshot = query_model_policy("SomeNode")
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
        with patch(_HANDLE, return_value=_success([ModelAccessVerdict("md_flux_dev", None, _DENIAL)])):
            snapshot = query_model_policy("SomeNode")
        assert snapshot.unmatchable_denials == ("md_flux_dev",)
        denial = snapshot.denial_for(ALLOWED, refuse_unrecognized=True)
        assert denial is not None
        # The escalation must reach the artist as an effect, not as a manifest instruction; the
        # catalog id and the fix belong in the log warning instead.
        assert "provider_model_id" not in denial.reason()
        assert "md_flux_dev" not in denial.reason()

    def test_a_permitted_handleless_entry_does_not_refuse_anything(self) -> None:
        """Only a DENIED unmatchable entry escalates; a permitted one is merely unmatchable."""
        with patch(_HANDLE, return_value=_success([ModelAccessVerdict("md_no_handle", None, None)])):
            snapshot = query_model_policy("SomeNode")
        assert snapshot.unmatchable_denials == ()
        assert snapshot.has_unmatchable_entries is True
        assert snapshot.denial_for(ALLOWED, refuse_unrecognized=True) is None

    def test_it_applies_even_with_refuse_unrecognized_off(self) -> None:
        """A static dropdown must not run a model policy explicitly forbade either."""
        with patch(_HANDLE, return_value=_success([ModelAccessVerdict("md_flux_dev", None, _DENIAL)])):
            snapshot = query_model_policy("SomeNode")
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
        with patch(_HANDLE, return_value=_success(verdicts)):
            snapshot = query_model_policy("SomeNode")
        assert snapshot.denial_for(shared) is _DENIAL

    def test_every_catalog_id_behind_a_handle_is_retained(self) -> None:
        """Callers that re-ask policy live must ask about all of them, not whichever was last."""
        shared = "black-forest-labs/FLUX.1-dev"
        verdicts = [
            ModelAccessVerdict(model_id="md_flux_byok", provider_model_id=shared, denial=None),
            ModelAccessVerdict(model_id="md_flux_gtc", provider_model_id=shared, denial=None),
        ]
        with patch(_HANDLE, return_value=_success(verdicts)):
            snapshot = query_model_policy("SomeNode")
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
        from tests.unit.exe_types.mocks import MockNode

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
        module = "griptape_nodes.exe_types.param_components"
        with (
            patch(f"{module}.model_policy.GriptapeNodes.handle_request", side_effect=handle),
            patch(f"{module}.model_access_component.GriptapeNodes.handle_request", side_effect=handle),
        ):
            component = ModelAccessComponent(
                node=node, parameter=parameter, model_choices=["alpha"], default_model="alpha"
            )
            # Decoration and the run gate must not disagree.
            assert component._cached_denial("alpha") is not None
            assert component.query_for_denial("alpha") is not None
            with pytest.raises(RuntimeError):
                component.raise_if_denied("alpha")


class TestConstructionDeferral:
    """During node __init__ the query is skipped entirely -- see reentrant-bus-in-init.

    A bus request issued while a node __init__ is on the stack deadlocks the worker's schema
    probe and gets the class dropped from the worker schema, so `query_model_policy` must not
    touch the bus there. The deferred snapshot it returns instead must deny nothing: no query
    was made, so there is no verdict (and no failure) to enforce.
    """

    def test_no_bus_request_while_constructing(self) -> None:
        handle = MagicMock()
        with patch(_HANDLE, handle), LibraryRegistry.constructing_node():
            snapshot = query_model_policy("SomeNode")
        handle.assert_not_called()
        assert snapshot.deferred is True

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
        with patch(_HANDLE, return_value=_success(verdicts)):
            with LibraryRegistry.constructing_node():
                assert query_model_policy("SomeNode").deferred is True
            snapshot = query_model_policy("SomeNode")
        assert snapshot.deferred is False
        assert snapshot.denial_for(DENIED) is _DENIAL


class TestSnapshotIsImmutable:
    def test_frozen(self) -> None:
        """Callers replace the snapshot wholesale, so the tables cannot drift apart."""
        snapshot = ModelPolicySnapshot()
        with pytest.raises(FrozenInstanceError):
            snapshot.failure_detail = "mutated"  # type: ignore[misc]
