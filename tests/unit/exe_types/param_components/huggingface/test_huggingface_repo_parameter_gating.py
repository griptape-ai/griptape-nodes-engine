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

from griptape_nodes.exe_types.param_components.huggingface.huggingface_model_parameter import NO_MODELS_PLACEHOLDER
from griptape_nodes.exe_types.param_components.huggingface.huggingface_repo_parameter import HuggingFaceRepoParameter
from griptape_nodes.node_library.library_registry import LibraryRegistry
from griptape_nodes.retained_mode.events.access_events import (
    ModelAccessVerdict,
    QueryModelAccessForNodeRequest,
    QueryModelAccessForNodeResultFailure,
    QueryModelAccessForNodeResultSuccess,
)
from griptape_nodes.retained_mode.events.model_events import ListModelDownloadsRequest
from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure
from tests.unit.exe_types.mocks import MockNode
from tests.unit.exe_types.param_components.probe_scope import constructing_under_probe

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
        patch("griptape_nodes.retained_mode.engine.Engine.handle_request", side_effect=handle_request),
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
        with patch(
            "griptape_nodes.retained_mode.engine.Engine.handle_request",
            return_value=QueryModelAccessForNodeResultFailure(result_details="node type not found"),
        ):
            param = _param(gated=True)
        denial = param.query_for_denial(ALLOWED_REPO)
        assert denial is not None
        assert "could not be checked against your license" in denial.reason()

    def test_raise_if_denied_raises_for_denied_and_passes_for_allowed(self) -> None:
        param = _param(gated=True)
        with pytest.raises(RuntimeError, match="not permitted"):
            param.raise_if_denied(DENIED_REPO)
        param.raise_if_denied(ALLOWED_REPO)


class TestTheEmptyCachePlaceholderIsNotBadgedAsUnlicensed:
    """Fail-closed applies to models, not to the row that says there are none.

    With an empty cache the dropdown falls back to the placeholder, and if the access query is
    also unanswerable, every whole-parameter refusal is live at once. Badging that row red says
    the artist's license forbids something, when what actually happened is a library-registration
    problem -- and it buries `get_repo_revision()`'s accurate "no such model" report under a
    licensing error, which points them at the wrong thing entirely.
    """

    def _unresolvable_param(self, *, add_parameters: bool = False) -> HuggingFaceRepoParameter:
        with patch(
            "griptape_nodes.retained_mode.engine.Engine.handle_request",
            return_value=QueryModelAccessForNodeResultFailure(result_details="node type not found"),
        ):
            # `repo_ids=[]` + `list_all_models=True` is the "offer whatever is cached" config, and
            # the cache scan is stubbed empty, so the placeholder is what gets selected.
            param = HuggingFaceRepoParameter(MockNode(), repo_ids=[], list_all_models=True, gated=True)
            if add_parameters:
                param.add_input_parameters()
            return param

    def test_the_placeholder_is_not_denied(self) -> None:
        param = self._unresolvable_param()
        # Fail-closed still holds for a real repo id -- only the non-model row is exempt.
        assert param.query_for_denial(ALLOWED_REPO) is not None
        assert param.query_for_denial(NO_MODELS_PLACEHOLDER) is None

    def test_no_badge_lands_on_the_placeholder_row(self) -> None:
        param = self._unresolvable_param(add_parameters=True)
        parameter = param._node.get_parameter_by_name("model")
        assert parameter is not None
        assert param._node.get_parameter_value("model") == NO_MODELS_PLACEHOLDER
        assert parameter.get_badge() is None

    def test_the_run_error_is_about_the_missing_model_not_the_license(self) -> None:
        param = self._unresolvable_param(add_parameters=True)
        errors = param.validate_before_node_run()
        assert errors is not None
        message = str(errors[0])
        assert "could not be checked against your license" not in message
        assert "not found in available models" in message


class TestUngatedIsUnchanged:
    """`gated=False` opts out entirely; such a parameter must behave exactly as before."""

    def test_ungated_never_denies(self) -> None:
        param = _param(gated=False, repo_ids=[ALLOWED_REPO, DENIED_REPO, UNDECLARED_REPO])
        assert param.query_for_denial(DENIED_REPO) is None
        assert param.query_for_denial(UNDECLARED_REPO) is None

    def test_ungated_does_not_query_policy(self) -> None:
        with patch("griptape_nodes.retained_mode.engine.Engine.handle_request") as handle:
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
        with patch(
            "griptape_nodes.retained_mode.engine.Engine.handle_request",
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
        with patch(
            "griptape_nodes.retained_mode.engine.Engine.handle_request",
            return_value=QueryModelAccessForNodeResultFailure(result_details="node type not found"),
        ):
            param = _param(gated=None)
        assert param._gated is True
        assert param.query_for_denial(DENIED_REPO) is not None

    def test_stays_ungated_for_a_library_that_declares_nothing(self) -> None:
        """The real pre-adoption path: a Success carrying no verdicts leaves gating off."""
        with patch(
            "griptape_nodes.retained_mode.engine.Engine.handle_request",
            return_value=QueryModelAccessForNodeResultSuccess(verdicts=[], result_details="ok"),
        ):
            param = _param(gated=None)
        assert param._gated is False
        assert param.query_for_denial(DENIED_REPO) is None

    def test_explicit_true_still_fails_closed_on_resolution_failure(self) -> None:
        """Opting in explicitly keeps the loud behavior auto-detect deliberately softens."""
        with patch(
            "griptape_nodes.retained_mode.engine.Engine.handle_request",
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


class TestConstructionDefersBusRequests:
    """No bus request may be issued from a node __init__ that a strict-mode scope is watching.

    The worker's schema probe constructs every node class during library load, inside a
    LOAD_PROBE scope; a bus request from inside that construction fires reentrant-bus-in-init
    and drops the class from the worker schema. So under a scope this component's policy and
    download queries defer until the first post-construction refresh.

    Construction with no scope open is the ordinary case and must still query -- see
    ``TestConstructionWithoutAScopeStillQueries``.
    """

    def _construct_deferred(self, *, gated: bool | None) -> HuggingFaceRepoParameter:
        with constructing_under_probe():
            param = _param(gated=gated)
            param.add_input_parameters()
        return param

    def test_construction_issues_no_bus_requests(self) -> None:
        with patch("griptape_nodes.retained_mode.engine.Engine.handle_request") as handle:
            self._construct_deferred(gated=True)
        handle.assert_not_called()

    def test_a_deferred_gated_param_denies_nothing(self) -> None:
        """No denials while deferred -- including the refuse-unrecognized backstop.

        A naive skip that left a plain empty snapshot would refuse EVERY choice as
        unrecognized and badge the whole dropdown "not permitted" at construction.
        """
        param = self._construct_deferred(gated=True)
        assert param._policy.deferred is True
        assert param.query_for_denial(DENIED_REPO) is None
        assert param.query_for_denial(UNDECLARED_REPO) is None

    def test_no_denial_decoration_lands_while_deferred(self) -> None:
        param = self._construct_deferred(gated=True)
        data = param._build_data_choices([ALLOWED_REPO, DENIED_REPO])
        assert all(row.get("icon") != "shield-off" for row in data)
        parameter = param._node.get_parameter_by_name("model")
        assert parameter is not None
        assert parameter.get_badge() is None

    def test_refresh_parameters_heals_after_construction(self) -> None:
        param = self._construct_deferred(gated=True)
        param.refresh_parameters()
        assert param._policy.deferred is False
        assert param.query_for_denial(DENIED_REPO) is not None
        assert param.query_for_denial(ALLOWED_REPO) is None

    def test_auto_detect_heals_too(self) -> None:
        """Pins the `_gate_mode is not False` refresh guard: auto-detect must re-query as well."""
        param = self._construct_deferred(gated=None)
        param.refresh_parameters()
        assert param._policy.deferred is False
        assert param.query_for_denial(DENIED_REPO) is not None

    def test_the_run_path_still_blocks_a_denied_selection(self) -> None:
        """validate_before_node_run refreshes first, so deferral cannot weaken run gating."""
        param = self._construct_deferred(gated=True)
        param._node.set_parameter_value("model", DENIED_REPO)
        errors = param.validate_before_node_run()
        assert errors is not None
        assert "not permitted" in str(errors[0])


class TestConstructionWithoutAScopeStillQueries:
    """An editor drop or workflow load queries from __init__, so decoration is right immediately.

    Outside a strict-mode scope nothing records a reentrant-bus-in-init violation and there is no
    probe to drop the class, so deferring there bought nothing -- and cost the shield rows and the
    download subtitles until the node was run or refreshed by hand. These pin that the deferral
    keys off the scope and not off being in ``__init__`` at all.
    """

    def _construct_in_init(self, *, gated: bool | None = True) -> HuggingFaceRepoParameter:
        with LibraryRegistry.constructing_node():
            param = _param(gated=gated)
            param.add_input_parameters()
        return param

    def test_policy_is_queried_during_construction(self) -> None:
        param = self._construct_in_init()
        assert param._policy.deferred is False
        assert param.query_for_denial(DENIED_REPO) is not None
        assert param.query_for_denial(ALLOWED_REPO) is None

    def test_denial_decoration_lands_without_a_refresh(self) -> None:
        param = self._construct_in_init()
        by_name = {row["name"]: row for row in param._build_data_choices([ALLOWED_REPO, DENIED_REPO])}
        assert by_name[DENIED_REPO]["icon"] == "shield-off"
        assert by_name[DENIED_REPO]["subtitle"] == "Not permitted by your license"

    def test_the_download_status_query_is_issued_too(self) -> None:
        """The other half of the decoration: without this the rows lose their download subtitle."""
        seen: list[type] = []

        def handle_request(request: object) -> object:
            seen.append(type(request))
            if isinstance(request, QueryModelAccessForNodeRequest):
                return QueryModelAccessForNodeResultSuccess(verdicts=_verdicts(), result_details="ok")
            return object()  # not a ResultSuccess -> "nothing downloading", as in _stub_engine

        with patch("griptape_nodes.retained_mode.engine.Engine.handle_request", side_effect=handle_request):
            self._construct_in_init()
        assert ListModelDownloadsRequest in seen
