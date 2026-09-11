"""Regression tests for license gating across every HuggingFace parameter subclass.

Gating originally lived only on `HuggingFaceRepoParameter`, and each of these cases is a bug that
shipped because the other subclasses were not exercised:

  - `HuggingFaceRepoVariantParameter` renders a third key shape (``owner/repo/variant``) that the
    base normalization did not recognize, so every variant row -- permitted ones included -- was
    refused as undeclared.
  - `HuggingFaceRepoFileParameter` reimplemented `refresh_parameters` and omitted the policy
    re-query, so it enforced against a snapshot frozen at construction.
  - A catalog `Model` may declare no `provider_model_id`; dropping those verdicts reclassified
    declared, permitted models as undeclared and hard-denied them.
  - The badge was not cleared when auto-detect flipped gating off, stranding a red "not permitted"
    badge on a model that now runs fine.
"""

from collections.abc import Iterator
from unittest.mock import patch

import pytest

from griptape_nodes.exe_types.param_components.huggingface.huggingface_repo_file_parameter import (
    HuggingFaceRepoFileParameter,
)
from griptape_nodes.exe_types.param_components.huggingface.huggingface_repo_parameter import HuggingFaceRepoParameter
from griptape_nodes.exe_types.param_components.huggingface.huggingface_repo_variant_parameter import (
    HuggingFaceRepoVariantParameter,
)
from griptape_nodes.retained_mode.events.access_events import (
    ModelAccessVerdict,
    QueryModelAccessForNodeRequest,
    QueryModelAccessForNodeResultSuccess,
)
from griptape_nodes.retained_mode.events.model_events import ListModelDownloadsRequest
from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure
from tests.unit.exe_types.mocks import MockNode

_MODULE = "griptape_nodes.exe_types.param_components.huggingface"

VARIANT_REPO = "Lightricks/LTX-2"
VARIANTS = ["ltx-2-19b-dev", "ltx-2-19b-dev-fp8"]

FILE_REPO = "black-forest-labs/FLUX.1-dev"
FILE_NAME = "flux1-dev.safetensors"

_DENIAL = CheckpointDenial(failures=(CheckpointFailure(detail="Provider forbidden by your license."),))


def _stub(verdicts: list[ModelAccessVerdict]):  # noqa: ANN202
    """Route access queries to canned verdicts and stub every cache scan."""

    def handle_request(request: object) -> object:
        if isinstance(request, QueryModelAccessForNodeRequest):
            return QueryModelAccessForNodeResultSuccess(verdicts=verdicts, result_details="ok")
        if isinstance(request, ListModelDownloadsRequest):
            return object()  # not a ResultSuccess -> "nothing downloading"
        msg = f"unexpected request: {type(request).__name__}"
        raise AssertionError(msg)

    return patch("griptape_nodes.retained_mode.engine.Engine.handle_request", side_effect=handle_request)


@pytest.fixture(autouse=True)
def _stub_caches() -> Iterator[None]:
    with (
        patch(f"{_MODULE}.huggingface_repo_parameter.list_repo_revisions_in_cache", return_value=[]),
        patch(f"{_MODULE}.huggingface_repo_parameter.list_all_repo_revisions_in_cache", return_value=[]),
        patch(f"{_MODULE}.huggingface_repo_file_parameter.list_repo_revisions_with_file_in_cache", return_value=[]),
        patch(f"{_MODULE}.huggingface_repo_variant_parameter._list_variants_in_cache", return_value=[]),
    ):
        yield


class TestVariantParameterKeyShape:
    """`owner/repo/variant` must reduce to the `owner/repo` a catalog declares."""

    def test_permitted_variant_is_not_refused_as_undeclared(self) -> None:
        verdicts = [ModelAccessVerdict(model_id="md_ltx_2", provider_model_id=VARIANT_REPO, denial=None)]
        with _stub(verdicts):
            param = HuggingFaceRepoVariantParameter(MockNode(), repo_id=VARIANT_REPO, variants=VARIANTS)
            for variant in VARIANTS:
                choice = f"{VARIANT_REPO}/{variant}"
                assert param.repo_id_for_choice(choice) == VARIANT_REPO
                assert param.query_for_denial(choice) is None, f"{choice} was refused"

    def test_denied_base_repo_denies_all_its_variants(self) -> None:
        verdicts = [ModelAccessVerdict(model_id="md_ltx_2", provider_model_id=VARIANT_REPO, denial=_DENIAL)]
        with _stub(verdicts):
            param = HuggingFaceRepoVariantParameter(MockNode(), repo_id=VARIANT_REPO, variants=VARIANTS)
            for variant in VARIANTS:
                assert param.query_for_denial(f"{VARIANT_REPO}/{variant}") is not None

    def test_placeholder_is_not_treated_as_a_model(self) -> None:
        verdicts = [ModelAccessVerdict(model_id="md_ltx_2", provider_model_id=VARIANT_REPO, denial=None)]
        with _stub(verdicts):
            param = HuggingFaceRepoVariantParameter(MockNode(), repo_id=VARIANT_REPO, variants=VARIANTS)
            assert param.repo_id_for_choice("No models downloaded — visit Model Manager") is None

    def test_a_bare_repo_id_is_not_split_into_its_owner(self) -> None:
        """A two-segment `owner/repo` must survive whole.

        Splitting on the last slash would yield the owner alone (`Lightricks`), which matches no
        catalog entry, so a value saved before this parameter offered variants would be refused
        with an error naming a "model" that is really just an org.
        """
        verdicts = [ModelAccessVerdict(model_id="md_ltx_2", provider_model_id=VARIANT_REPO, denial=None)]
        with _stub(verdicts):
            param = HuggingFaceRepoVariantParameter(MockNode(), repo_id=VARIANT_REPO, variants=VARIANTS)
            assert param.repo_id_for_choice(VARIANT_REPO) == VARIANT_REPO
            assert param.query_for_denial(VARIANT_REPO) is None
            assert param._key_to_repo_variant(VARIANT_REPO) == (VARIANT_REPO, "")


class TestEverySubclassRequeriesPolicy:
    """Policy must be re-queried on refresh in every subclass, not just the one with tests."""

    @pytest.mark.parametrize("subclass", ["repo", "file", "variant"])
    def test_refresh_parameters_requeries_policy(self, subclass: str) -> None:
        verdicts = [ModelAccessVerdict(model_id="md_a", provider_model_id=FILE_REPO, denial=None)]
        with _stub(verdicts) as handle:
            if subclass == "repo":
                param = HuggingFaceRepoParameter(MockNode(), repo_ids=[FILE_REPO])
            elif subclass == "file":
                param = HuggingFaceRepoFileParameter(MockNode(), repo_files=[(FILE_REPO, FILE_NAME)])
            else:
                param = HuggingFaceRepoVariantParameter(MockNode(), repo_id=VARIANT_REPO, variants=VARIANTS)
            param.add_input_parameters()
            before = sum(
                1 for call in handle.call_args_list if isinstance(call.args[0], QueryModelAccessForNodeRequest)
            )
            param.refresh_parameters()
            after = sum(1 for call in handle.call_args_list if isinstance(call.args[0], QueryModelAccessForNodeRequest))
        assert after > before, f"{subclass} did not re-query policy during refresh_parameters()"

    @pytest.mark.parametrize("subclass", ["repo", "file", "variant"])
    def test_a_license_change_between_refreshes_takes_effect(self, subclass: str) -> None:
        """The point of re-querying: a newly denied model must start being refused.

        Each subclass is probed with a choice string in ITS OWN key shape, since that is what a
        real dropdown would hand to `query_for_denial`.
        """
        if subclass == "repo":
            build = lambda: HuggingFaceRepoParameter(MockNode(), repo_ids=[FILE_REPO])  # noqa: E731
            declared, probe = FILE_REPO, FILE_REPO
        elif subclass == "file":
            build = lambda: HuggingFaceRepoFileParameter(MockNode(), repo_files=[(FILE_REPO, FILE_NAME)])  # noqa: E731
            declared, probe = FILE_REPO, FILE_REPO
        else:
            build = lambda: HuggingFaceRepoVariantParameter(  # noqa: E731
                MockNode(), repo_id=VARIANT_REPO, variants=VARIANTS
            )
            declared, probe = VARIANT_REPO, f"{VARIANT_REPO}/{VARIANTS[0]}"

        allowed = [ModelAccessVerdict(model_id="md_a", provider_model_id=declared, denial=None)]
        denied = [ModelAccessVerdict(model_id="md_a", provider_model_id=declared, denial=_DENIAL)]
        with _stub(allowed):
            param = build()
            param.add_input_parameters()
            assert param.query_for_denial(probe) is None
        with _stub(denied):
            param.refresh_parameters()
        assert param.query_for_denial(probe) is not None


class TestOptionalProviderModelId:
    """`provider_model_id` is optional on a catalog Model; absence is not "unresolved".

    An entry without one is declared but unmatchable against a cache-derived choice string. That
    makes the repo-id table an INCOMPLETE view of the catalog, so absence from it no longer proves
    a repo is undeclared -- refusing on it would block models the author did declare and tell them
    to declare something they already had.
    """

    def test_a_model_declaring_no_provider_model_id_does_not_disable_gating(self) -> None:
        """Explicit denials still apply: an unmatchable sibling must not switch enforcement off."""
        verdicts = [
            ModelAccessVerdict(model_id="md_no_handle", provider_model_id=None, denial=None),
            ModelAccessVerdict(model_id="md_denied", provider_model_id=FILE_REPO, denial=_DENIAL),
        ]
        with _stub(verdicts):
            param = HuggingFaceRepoParameter(MockNode(), repo_ids=[FILE_REPO])
        assert param._gated is True
        assert param.query_for_denial(FILE_REPO) is not None

    def test_an_unmatchable_entry_stops_undeclared_refusals(self) -> None:
        """The catalog covers something the table cannot represent, so absence proves nothing."""
        verdicts = [
            ModelAccessVerdict(model_id="md_no_handle", provider_model_id=None, denial=None),
            ModelAccessVerdict(model_id="md_a", provider_model_id=FILE_REPO, denial=None),
        ]
        with _stub(verdicts):
            param = HuggingFaceRepoParameter(MockNode(), repo_ids=[FILE_REPO])
        assert param._policy.has_unmatchable_entries is True
        assert param.query_for_denial("someone/undeclared") is None

    def test_sibling_models_with_handles_are_unaffected(self) -> None:
        verdicts = [
            ModelAccessVerdict(model_id="md_no_handle", provider_model_id=None, denial=None),
            ModelAccessVerdict(model_id="md_a", provider_model_id=FILE_REPO, denial=None),
        ]
        with _stub(verdicts):
            param = HuggingFaceRepoParameter(MockNode(), repo_ids=[FILE_REPO])
        assert param.query_for_denial(FILE_REPO) is None

    def test_a_fully_matchable_catalog_still_refuses_undeclared_repos(self) -> None:
        """The fail-closed path is preserved when the table IS a complete picture."""
        verdicts = [ModelAccessVerdict(model_id="md_a", provider_model_id=FILE_REPO, denial=None)]
        with _stub(verdicts):
            param = HuggingFaceRepoParameter(MockNode(), repo_ids=[FILE_REPO])
        assert param._policy.has_unmatchable_entries is False
        assert param.query_for_denial("someone/undeclared") is not None


class TestListAllModelsIsNotRefusedWholesale:
    """`list_all_models=True` offers the whole local cache, which no catalog can enumerate."""

    def test_undeclared_cached_repos_are_allowed(self) -> None:
        verdicts = [ModelAccessVerdict(model_id="md_a", provider_model_id=FILE_REPO, denial=None)]
        with _stub(verdicts):
            param = HuggingFaceRepoParameter(MockNode(), repo_ids=[FILE_REPO], list_all_models=True)
        assert param.offers_only_declared_repos() is False
        assert param.query_for_denial("someone/pulled-this-down-themselves") is None

    def test_explicit_denials_still_apply(self) -> None:
        """Opting out of undeclared-refusal must not opt out of real policy denials."""
        verdicts = [ModelAccessVerdict(model_id="md_a", provider_model_id=FILE_REPO, denial=_DENIAL)]
        with _stub(verdicts):
            param = HuggingFaceRepoParameter(MockNode(), repo_ids=[FILE_REPO], list_all_models=True)
        assert param.query_for_denial(FILE_REPO) is not None


class TestBadgeIsClearedWhenGatingTurnsOff:
    def test_badge_does_not_survive_a_gate_flipping_off(self) -> None:
        """Auto-detect can legitimately flip gating off; a stale red badge must not persist."""
        denied = [ModelAccessVerdict(model_id="md_a", provider_model_id=FILE_REPO, denial=_DENIAL)]
        with _stub(denied):
            param = HuggingFaceRepoParameter(MockNode(), repo_ids=[FILE_REPO])
            param.add_input_parameters()
            param._node.set_parameter_value("model", FILE_REPO)
            param.refresh_parameters()
        parameter = param._node.get_parameter_by_name("model")
        assert parameter is not None
        assert parameter._badge is not None, "expected a denial badge while gated"

        # Library stops declaring models for this node type -> auto-detect turns gating off.
        with _stub([]):
            param.refresh_parameters()
        assert parameter._badge is None, "badge survived the gate turning off"


class TestDeprecatedFilteringIsSharedNotDuplicated:
    """`filter_choices` lives on the base so the subclasses cannot drift apart.

    It was previously copy-pasted into two subclasses and had already diverged: only one kept the
    empty-result fallback, so the other overwrote the node's stored selection with the placeholder.
    """

    @pytest.mark.parametrize("subclass", ["repo", "file"])
    def test_all_deprecated_offers_them_rather_than_emptying_the_dropdown(self, subclass: str) -> None:
        """An empty dropdown would clobber the stored value; a deprecated row is the lesser evil.

        Reachable when a deprecated repo is the only one in the local cache: it is offered as a
        choice, then filtered out for being deprecated, leaving nothing. Both subclasses must fall
        back to offering it rather than writing the placeholder over the artist's selection.
        """
        verdicts = [ModelAccessVerdict(model_id="md_a", provider_model_id=FILE_REPO, denial=None)]
        with _stub(verdicts):
            if subclass == "repo":
                param = HuggingFaceRepoParameter(MockNode(), repo_ids=[], deprecated_repo_ids=[FILE_REPO])
            else:
                param = HuggingFaceRepoFileParameter(
                    MockNode(), repo_files=[], deprecated_repo_files=[(FILE_REPO, FILE_NAME)]
                )
            # The deprecated repo IS cached, so get_choices() offers it.
            with patch.object(type(param), "get_choices", return_value=[FILE_REPO]):
                param.add_input_parameters()
                assert param.filter_choices([FILE_REPO], None) == [FILE_REPO]
                assert param._node.get_parameter_value("model") == FILE_REPO

    @pytest.mark.parametrize("subclass", ["repo", "file"])
    def test_a_deprecated_repo_is_hidden_when_something_else_is_available(self, subclass: str) -> None:
        verdicts = [ModelAccessVerdict(model_id="md_a", provider_model_id=FILE_REPO, denial=None)]
        keep = "owner/current"
        with _stub(verdicts):
            if subclass == "repo":
                param = HuggingFaceRepoParameter(MockNode(), repo_ids=[keep], deprecated_repo_ids=[FILE_REPO])
            else:
                param = HuggingFaceRepoFileParameter(
                    MockNode(), repo_files=[(keep, FILE_NAME)], deprecated_repo_files=[(FILE_REPO, FILE_NAME)]
                )
            assert param.filter_choices([keep, FILE_REPO], None) == [keep]
            # ...unless it is the active selection, which must not be silently retargeted.
            assert param.filter_choices([keep, FILE_REPO], FILE_REPO) == [keep, FILE_REPO]


class TestIncomingSelectionIsHonored:
    def test_value_being_set_survives_a_refresh(self) -> None:
        """`refresh_parameters(value_being_set=x)` must keep x, not fall back to the stored value.

        This is the `after_value_set` path: the parameter still holds the OLD value, so a default
        picker that re-reads storage would discard the selection the artist just made.
        """
        other = "owner/other"
        verdicts = [
            ModelAccessVerdict(model_id="md_a", provider_model_id=FILE_REPO, denial=None),
            ModelAccessVerdict(model_id="md_b", provider_model_id=other, denial=None),
        ]
        with _stub(verdicts):
            param = HuggingFaceRepoParameter(MockNode(), repo_ids=[FILE_REPO, other])
            param.add_input_parameters()
            param._node.set_parameter_value("model", FILE_REPO)
            param.refresh_parameters(value_being_set=other)
        assert param._node.get_parameter_value("model") == other


class TestAnEmptyCacheDoesNotDestroyASavedSelection:
    """Opening a saved workflow on a machine that lacks the model must not lose the model's name.

    The old per-subclass override returned early when nothing was cached, leaving the stored value
    intact. The template method always writes, so an empty scan would replace a real repo id with
    the placeholder string -- and re-saving would persist that loss, turning "you need to download
    X" into "model 'No models downloaded — visit Model Manager' not found".
    """

    @pytest.mark.parametrize("subclass", ["repo", "file"])
    def test_the_stored_repo_id_survives_a_refresh_with_nothing_cached(self, subclass: str) -> None:
        verdicts = [ModelAccessVerdict(model_id="md_a", provider_model_id=FILE_REPO, denial=None)]
        with _stub(verdicts):
            if subclass == "repo":
                param = HuggingFaceRepoParameter(MockNode(), repo_ids=[FILE_REPO])
            else:
                param = HuggingFaceRepoFileParameter(MockNode(), repo_files=[(FILE_REPO, FILE_NAME)])
            param.add_input_parameters()
            param._node.set_parameter_value("model", FILE_REPO)
            # Nothing cached AND nothing offered for download -> no choices at all.
            with patch.object(type(param), "get_choices", return_value=[]):
                param.refresh_parameters()
        assert param._node.get_parameter_value("model") == FILE_REPO


class TestEnforcementDecisionCannotDriftFromTheSnapshot:
    """`_gated` is derived from the snapshot on read, never stored beside it.

    Storing it would mean two values that must be updated together, and a refresh that assigned one
    but not the other would enforce against verdicts it no longer holds -- or stop enforcing while
    denials are still live. Deriving makes that unrepresentable.
    """

    def test_gating_follows_a_snapshot_replacement_with_no_second_assignment(self) -> None:
        verdicts = [ModelAccessVerdict(model_id="md_a", provider_model_id=FILE_REPO, denial=_DENIAL)]
        with _stub(verdicts):
            param = HuggingFaceRepoParameter(MockNode(), repo_ids=[FILE_REPO])
        assert param._gated is True
        assert param.query_for_denial(FILE_REPO) is not None

        # The library stops declaring models for this node type. `_refresh_policy` assigns ONLY
        # `_policy`; enforcement must follow without anything else being touched.
        with _stub([]):
            param._refresh_policy()
        assert param._gated is False
        assert param.query_for_denial(FILE_REPO) is None

    def test_gated_is_read_only(self) -> None:
        """A settable flag is the drift vector; keep it derived."""
        verdicts = [ModelAccessVerdict(model_id="md_a", provider_model_id=FILE_REPO, denial=None)]
        with _stub(verdicts):
            param = HuggingFaceRepoParameter(MockNode(), repo_ids=[FILE_REPO])
        with pytest.raises(AttributeError):
            param._gated = False  # type: ignore[misc]

    @pytest.mark.parametrize("mode", [True, False], ids=["explicit-on", "explicit-off"])
    def test_an_explicit_mode_ignores_what_the_catalog_declares(self, *, mode: bool) -> None:
        """Only auto-detect reads the snapshot; an explicit choice is absolute.

        Stubbed with no verdicts, so auto-detect would resolve to off -- `gated=True` must still
        enforce, and `gated=False` must still not.
        """
        with _stub([]):
            param = HuggingFaceRepoParameter(MockNode(), repo_ids=[FILE_REPO], gated=mode)
        assert param._gated is mode


class TestPolicySnapshotIsAtomic:
    def test_a_failed_requery_replaces_the_whole_snapshot(self) -> None:
        """Denials from a previous query must not linger next to a new failure verdict."""
        denied = [ModelAccessVerdict(model_id="md_a", provider_model_id=FILE_REPO, denial=_DENIAL)]
        with _stub(denied):
            param = HuggingFaceRepoParameter(MockNode(), repo_ids=[FILE_REPO], gated=True)
        first = param._policy
        assert first.denial_by_provider_id != {}
        assert first.catalog_ids_by_provider_id != {}

        with _stub([]):
            param._refresh_policy()
        second = param._policy
        assert second is not first, "snapshot was mutated in place rather than replaced"
        assert second.denial_by_provider_id == {}
        assert second.catalog_ids_by_provider_id == {}
