"""Tests for the policy layer shared by the two model-dropdown components.

`ModelAccessComponent` (static, author-enumerated choices) and `HuggingFaceModelParameter`
(choices from a local cache scan) own their `Parameter` differently and do not compose, but they
must never answer "is this model permitted?" differently. These tests pin the shared contract, and
in particular the one axis on which the two are ALLOWED to differ: `refuse_unrecognized`.
"""

from dataclasses import FrozenInstanceError
from unittest.mock import patch

import pytest

from griptape_nodes.exe_types.param_components.model_policy import (
    ModelPolicySnapshot,
    query_model_policy,
)
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
        assert snapshot.catalog_id_by_provider_id == {DENIED: "md_denied", ALLOWED: "md_allowed"}
        assert snapshot.failure_detail is None
        assert snapshot.has_unmatchable_entries is False

    def test_fail_closed_records_a_failure_detail(self) -> None:
        with patch(_HANDLE, return_value=QueryModelAccessForNodeResultFailure(result_details="not found")):
            snapshot = query_model_policy("SomeNode")
        assert snapshot.failure_detail is not None
        assert "SomeNode" in snapshot.failure_detail

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
        assert snapshot.catalog_id_by_provider_id == {}
        # Still counts as declaring models -- otherwise enforcement would silently switch off.
        assert snapshot.declares_models is True


class TestDenialFor:
    def test_an_explicit_denial_is_returned(self) -> None:
        snapshot = ModelPolicySnapshot(denial_by_provider_id={DENIED: _DENIAL}, catalog_id_by_provider_id={DENIED: "x"})
        assert snapshot.denial_for(DENIED) is _DENIAL

    def test_a_permitted_model_is_allowed(self) -> None:
        snapshot = ModelPolicySnapshot(catalog_id_by_provider_id={ALLOWED: "x"})
        assert snapshot.denial_for(ALLOWED) is None

    def test_none_is_never_denied(self) -> None:
        """A placeholder row or a connected driver object is not a model."""
        snapshot = ModelPolicySnapshot(failure_detail=None, catalog_id_by_provider_id={ALLOWED: "x"})
        assert snapshot.denial_for(None, refuse_unrecognized=True) is None

    def test_a_failure_detail_denies_everything(self) -> None:
        snapshot = ModelPolicySnapshot(failure_detail="could not evaluate")
        assert snapshot.denial_for(ALLOWED) is not None

    def test_unrecognized_is_allowed_by_default(self) -> None:
        """The static-dropdown stance: an unknown id was vetted at authoring time."""
        snapshot = ModelPolicySnapshot(catalog_id_by_provider_id={ALLOWED: "x"})
        assert snapshot.denial_for(UNKNOWN) is None

    def test_unrecognized_is_refused_when_asked(self) -> None:
        """The cache-scan stance: an unknown repo could be anything the artist pulled down."""
        snapshot = ModelPolicySnapshot(catalog_id_by_provider_id={ALLOWED: "x"})
        denial = snapshot.denial_for(UNKNOWN, refuse_unrecognized=True)
        assert denial is not None
        assert "not declared" in denial.reason()

    def test_unrecognized_is_allowed_when_the_catalog_view_is_incomplete(self) -> None:
        """An unmatchable entry means absence proves nothing, so the refusal is suppressed.

        Without this, a library author who declared a model with only the two required fields would
        have it blocked, with an error telling them to declare what they already declared.
        """
        snapshot = ModelPolicySnapshot(catalog_id_by_provider_id={ALLOWED: "x"}, has_unmatchable_entries=True)
        assert snapshot.denial_for(UNKNOWN, refuse_unrecognized=True) is None

    def test_explicit_denials_survive_an_incomplete_view(self) -> None:
        """Suppressing undeclared-refusal must not suppress real policy denials."""
        snapshot = ModelPolicySnapshot(
            denial_by_provider_id={DENIED: _DENIAL},
            catalog_id_by_provider_id={DENIED: "x"},
            has_unmatchable_entries=True,
        )
        assert snapshot.denial_for(DENIED, refuse_unrecognized=True) is _DENIAL


class TestSnapshotIsImmutable:
    def test_frozen(self) -> None:
        """Callers replace the snapshot wholesale, so the tables cannot drift apart."""
        snapshot = ModelPolicySnapshot()
        with pytest.raises(FrozenInstanceError):
            snapshot.failure_detail = "mutated"  # type: ignore[misc]
