"""Tests for license gating on the HuggingFace repo dropdown.

The dropdown offers models discovered by scanning the local HuggingFace cache, so the choice
strings are not catalog ids: a repo with more than one cached revision renders as
``"owner/repo (<40-hex>)"``, and some providers append a ``::subvariant`` selector. Gating has to
normalize a choice back to its bare repo id before consulting policy, or a denied model reads as
unknown and slips through.

These tests pin the fail-closed contract:

  - a denied repo stays denied no matter which string shape it renders as
  - a repo absent from the catalog is refused rather than offered -- an undeclared model carries no
    ``ModelProvider`` parent edge, so no provider-scoped policy can reach it. This applies only
    when the catalog is a complete picture of what the parameter offers; see
    ``test_huggingface_subclass_gating.py`` for the cases where it is not and absence is allowed.
  - an ungated parameter keeps the historical "offer whatever is cached" behavior
"""

from collections.abc import Iterator
from unittest.mock import patch

import pytest

from griptape_nodes.exe_types.param_components.huggingface.huggingface_repo_parameter import HuggingFaceRepoParameter
from griptape_nodes.retained_mode.events.access_events import (
    ModelAccessVerdict,
    QueryModelAccessForNodeRequest,
    QueryModelAccessForNodeResultFailure,
    QueryModelAccessForNodeResultSuccess,
)
from griptape_nodes.retained_mode.events.model_events import ListModelDownloadsRequest
from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure
from tests.unit.exe_types.mocks import MockNode

DENIED_REPO = "black-forest-labs/FLUX.1-dev"
ALLOWED_REPO = "black-forest-labs/FLUX.1-schnell"
UNDECLARED_REPO = "someone/not-in-the-catalog"

_DENIAL = CheckpointDenial(failures=(CheckpointFailure(detail="Model is not permitted by your license."),))


def _verdicts() -> list[ModelAccessVerdict]:
    return [
        ModelAccessVerdict(model_id="md_flux_1_dev", provider_model_id=DENIED_REPO, denial=_DENIAL),
        ModelAccessVerdict(model_id="md_flux_1_schnell", provider_model_id=ALLOWED_REPO, denial=None),
    ]


@pytest.fixture(autouse=True)
def _stub_engine() -> Iterator[None]:
    """Stub the cache scan and route access/download requests to canned results."""

    def handle_request(request: object) -> object:
        if isinstance(request, QueryModelAccessForNodeRequest):
            return QueryModelAccessForNodeResultSuccess(verdicts=_verdicts(), result_details="ok")
        if isinstance(request, ListModelDownloadsRequest):
            return object()  # not a ResultSuccess -> treated as "nothing downloading"
        msg = f"unexpected request: {type(request).__name__}"
        raise AssertionError(msg)

    module = "griptape_nodes.exe_types.param_components.huggingface"
    with (
        patch(f"{module}.huggingface_repo_parameter.list_repo_revisions_in_cache", return_value=[]),
        patch(f"{module}.huggingface_repo_parameter.list_all_repo_revisions_in_cache", return_value=[]),
        patch(f"{module}.huggingface_model_parameter.GriptapeNodes.handle_request", side_effect=handle_request),
    ):
        yield


def _param(*, gated: bool | None, repo_ids: list[str] | None = None) -> HuggingFaceRepoParameter:
    return HuggingFaceRepoParameter(
        MockNode(),
        repo_ids=repo_ids if repo_ids is not None else [ALLOWED_REPO, DENIED_REPO],
        gated=gated,
    )


class TestChoiceNormalization:
    """`repo_id_for_choice` is the single normalization point every policy lookup goes through."""

    def test_strips_revision_hash(self) -> None:
        param = _param(gated=True)
        choice = f"{DENIED_REPO} ({'a' * 40})"
        assert param.repo_id_for_choice(choice) == DENIED_REPO

    def test_strips_subvariant_postfix(self) -> None:
        param = _param(gated=True)
        assert param.repo_id_for_choice("Lightricks/LTX-2::ltx-2-19b-dev") == "Lightricks/LTX-2"

    def test_placeholder_is_not_a_model(self) -> None:
        param = _param(gated=True)
        assert param.repo_id_for_choice("No models downloaded — visit Model Manager") is None
        assert param.repo_id_for_choice("") is None


class TestFailClosed:
    def test_denied_repo_is_denied(self) -> None:
        param = _param(gated=True)
        assert param.query_for_denial(DENIED_REPO) is not None

    def test_denied_repo_with_revision_hash_is_still_denied(self) -> None:
        """The revision-hash bypass: two cached revisions must not unblock a denied repo."""
        param = _param(gated=True)
        choice = f"{DENIED_REPO} ({'b' * 40})"
        assert param.query_for_denial(choice) is not None

    def test_undeclared_repo_is_refused(self) -> None:
        """An uncataloged repo cannot be matched by a provider-scoped rule, so it is not offered."""
        param = _param(gated=True, repo_ids=[ALLOWED_REPO, UNDECLARED_REPO])
        denial = param.query_for_denial(UNDECLARED_REPO)
        assert denial is not None
        assert UNDECLARED_REPO in denial.reason()

    def test_allowed_repo_is_permitted(self) -> None:
        param = _param(gated=True)
        assert param.query_for_denial(ALLOWED_REPO) is None

    def test_placeholder_is_never_denied(self) -> None:
        param = _param(gated=True)
        assert param.query_for_denial("No models downloaded — visit Model Manager") is None

    def test_policy_resolution_failure_denies_everything(self) -> None:
        """A library that cannot be resolved must not silently open the gate."""
        module = "griptape_nodes.exe_types.param_components.huggingface"
        with patch(
            f"{module}.huggingface_model_parameter.GriptapeNodes.handle_request",
            return_value=QueryModelAccessForNodeResultFailure(result_details="node type not found"),
        ):
            param = _param(gated=True)
        denial = param.query_for_denial(ALLOWED_REPO)
        assert denial is not None
        assert "could not be evaluated" in denial.reason()

    def test_raise_if_denied_raises_for_denied_and_passes_for_allowed(self) -> None:
        param = _param(gated=True)
        with pytest.raises(RuntimeError, match="not permitted"):
            param.raise_if_denied(DENIED_REPO)
        param.raise_if_denied(ALLOWED_REPO)


class TestUngatedIsUnchanged:
    """`gated=False` opts out entirely; such a parameter must behave exactly as before."""

    def test_ungated_never_denies(self) -> None:
        param = _param(gated=False, repo_ids=[ALLOWED_REPO, DENIED_REPO, UNDECLARED_REPO])
        assert param.query_for_denial(DENIED_REPO) is None
        assert param.query_for_denial(UNDECLARED_REPO) is None

    def test_ungated_does_not_query_policy(self) -> None:
        module = "griptape_nodes.exe_types.param_components.huggingface"
        with patch(f"{module}.huggingface_model_parameter.GriptapeNodes.handle_request") as handle:
            _param(gated=False)
        access_calls = [
            call for call in handle.call_args_list if isinstance(call.args[0], QueryModelAccessForNodeRequest)
        ]
        assert access_calls == []


class TestAutoDetect:
    """The default: gate when the library declares models for this node, otherwise don't.

    This is what lets an adopting library get enforcement without editing its Python, while a
    library that predates declarations keeps working unchanged.
    """

    def test_gates_when_the_node_declares_models(self) -> None:
        param = _param(gated=None)
        assert param.query_for_denial(DENIED_REPO) is not None

    def test_is_the_default(self) -> None:
        """Callers get gating without passing anything, which is what makes adoption free.

        `HuggingFaceRepoParameter.__init__` runs `refresh_parameters()` before detection has
        resolved, so a refresh guard keyed on the *resolved* flag rather than the configured mode
        would leave this permanently ungated. This pins that ordering.
        """
        param = HuggingFaceRepoParameter(MockNode(), repo_ids=[ALLOWED_REPO, DENIED_REPO])
        assert param.query_for_denial(DENIED_REPO) is not None
        assert param.query_for_denial(ALLOWED_REPO) is None

    def test_survives_a_refresh(self) -> None:
        """Gating must persist across refresh_parameters(), not just initial construction."""
        param = _param(gated=None)
        param.add_input_parameters()
        param.refresh_parameters()
        assert param.query_for_denial(DENIED_REPO) is not None

    def test_stays_ungated_when_the_node_declares_nothing(self) -> None:
        """An empty verdict list means no catalog to gate against, so offer everything."""
        module = "griptape_nodes.exe_types.param_components.huggingface"
        with patch(
            f"{module}.huggingface_model_parameter.GriptapeNodes.handle_request",
            return_value=QueryModelAccessForNodeResultSuccess(verdicts=[], result_details="ok"),
        ):
            param = _param(gated=None)
        assert param.query_for_denial(DENIED_REPO) is None
        assert param.query_for_denial(UNDECLARED_REPO) is None

    def test_fails_closed_when_the_node_type_cannot_be_resolved(self) -> None:
        """An unanswerable query must NOT read as "pre-adoption" -- auto-detect fails closed.

        A library that genuinely has not adopted declarations resolves *successfully* with an empty
        verdict list, which already leaves enforcement off. So a Failure means the node type could
        not be resolved at all (unregistered, ambiguous across two libraries, or mid-reload), and
        treating that as "allow everything" let an admin's deny be bypassed by a lookup error.
        """
        module = "griptape_nodes.exe_types.param_components.huggingface"
        with patch(
            f"{module}.huggingface_model_parameter.GriptapeNodes.handle_request",
            return_value=QueryModelAccessForNodeResultFailure(result_details="node type not found"),
        ):
            param = _param(gated=None)
        assert param._gated is True
        assert param.query_for_denial(DENIED_REPO) is not None

    def test_stays_ungated_for_a_library_that_declares_nothing(self) -> None:
        """The real pre-adoption path: a Success carrying no verdicts leaves gating off."""
        module = "griptape_nodes.exe_types.param_components.huggingface"
        with patch(
            f"{module}.huggingface_model_parameter.GriptapeNodes.handle_request",
            return_value=QueryModelAccessForNodeResultSuccess(verdicts=[], result_details="ok"),
        ):
            param = _param(gated=None)
        assert param._gated is False
        assert param.query_for_denial(DENIED_REPO) is None

    def test_explicit_true_still_fails_closed_on_resolution_failure(self) -> None:
        """Opting in explicitly keeps the loud behavior auto-detect deliberately softens."""
        module = "griptape_nodes.exe_types.param_components.huggingface"
        with patch(
            f"{module}.huggingface_model_parameter.GriptapeNodes.handle_request",
            return_value=QueryModelAccessForNodeResultFailure(result_details="node type not found"),
        ):
            param = _param(gated=True)
        assert param.query_for_denial(ALLOWED_REPO) is not None


class TestRowDecoration:
    def test_denied_rows_carry_the_entitlement_icon(self) -> None:
        param = _param(gated=True)
        data = param._build_data_choices([ALLOWED_REPO, DENIED_REPO])
        by_name = {row["name"]: row for row in data}
        assert by_name[DENIED_REPO]["icon"] == "shield-off"
        assert by_name[DENIED_REPO]["subtitle"] == "Not permitted by your license"

    def test_download_status_still_renders_for_permitted_rows(self) -> None:
        """Entitlement decoration must not erase the download affordance it shares keys with."""
        param = _param(gated=True)
        data = param._build_data_choices([ALLOWED_REPO, DENIED_REPO])
        by_name = {row["name"]: row for row in data}
        assert by_name[ALLOWED_REPO]["subtitle"] == "Not downloaded"


class TestRunPathGate:
    def test_validate_before_node_run_blocks_a_denied_selection(self) -> None:
        param = _param(gated=True)
        param.add_input_parameters()
        param._node.set_parameter_value("model", DENIED_REPO)
        errors = param.validate_before_node_run()
        assert errors is not None
        assert "not permitted" in str(errors[0])
