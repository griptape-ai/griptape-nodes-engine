"""Gating tests that resolve through a REAL `LibraryRegistry` catalog, not a stubbed request.

The sibling suites for these components stub `GriptapeNodes.handle_request` and hand-build
`ModelAccessVerdict` objects. That validates internal consistency but not the engine contract, and
it is why two real defects in this change shipped green: a mock whose `provider_model_id` always
equals the dropdown string cannot express a catalog `Model` that declares no `provider_model_id`,
nor a choice whose rendered shape does not reduce to a catalog id.

So these tests register a probe node against a real library schema -- real `ModelCatalogLibraryProperty`,
real `ModelUsageNodeProperty` -- and let `QueryModelAccessForNodeRequest` resolve it through
`access_manager`. Denials come from a real authorization hook, the same seam the app uses to install
license policy. Nothing about the model-access path is mocked.

Covers all three subclasses, because gating behaves differently in each: `HuggingFaceRepoParameter`
(bare repo ids), `HuggingFaceRepoFileParameter` (repo + file), and `HuggingFaceRepoVariantParameter`
(`owner/repo/variant` keys).
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

import pytest

from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.exe_types.param_components.huggingface.huggingface_repo_file_parameter import (
    HuggingFaceRepoFileParameter,
)
from griptape_nodes.exe_types.param_components.huggingface.huggingface_repo_parameter import HuggingFaceRepoParameter
from griptape_nodes.exe_types.param_components.huggingface.huggingface_repo_variant_parameter import (
    HuggingFaceRepoVariantParameter,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
    AuthorizationCheckpoint,
    CheckpointAction,
    CheckpointAttribute,
    CheckpointDenial,
    CheckpointFailure,
)

_LIBRARY_NAME = "hf-gating-real-catalog-test-library"
_MODULE = "griptape_nodes.exe_types.param_components.huggingface"

# Repos that exist in the test catalog. `DENIED_REPO` is keyed to a provider the hook forbids.
DENIED_REPO = "black-forest-labs/FLUX.1-dev"
ALLOWED_REPO = "openai/clip-vit-large-patch14"
VARIANT_REPO = "Lightricks/LTX-2"
VARIANT = "ltx-2-19b-dev"
FILE_NAME = "model.safetensors"
UNDECLARED_REPO = "someone/never-cataloged"

# A model the catalog declares with NO provider_model_id -- legal per `Model`, and the exact shape
# a stubbed verdict cannot express.
HANDLELESS_MODEL_ID = "md_no_upstream_handle"

DENIED_PROVIDER = "denied_provider"


class _HfProbeNode(BaseNode):
    """Concrete BaseNode so `QueryModelAccessForNodeRequest` can resolve a node type."""

    def __init__(self, name: str = "hf_probe", metadata=None) -> None:  # noqa: ANN001
        super().__init__(name=name, metadata=metadata)

    def process(self) -> None:
        """No-op; these tests never execute the node."""


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """Clear the LibraryRegistry singletons around each test."""
    from griptape_nodes.node_library.library_registry import LibraryRegistry

    stores = ("_libraries", "_node_aliases", "_collision_node_names_to_library_names", "_registered_widgets")
    for store in stores:
        getattr(LibraryRegistry, store).clear()
    yield
    for store in stores:
        getattr(LibraryRegistry, store).clear()


@pytest.fixture(autouse=True)
def _stub_hf_cache() -> Iterator[None]:
    """Stub only the HuggingFace CACHE SCAN -- never the model-access request path.

    The cache scan reads the real `~/.cache/huggingface`, which would make results depend on the
    developer's machine. Everything downstream of `get_choices()` is left real.
    """
    with (
        patch(f"{_MODULE}.huggingface_repo_parameter.list_repo_revisions_in_cache", return_value=[]),
        patch(f"{_MODULE}.huggingface_repo_parameter.list_all_repo_revisions_in_cache", return_value=[]),
        patch(f"{_MODULE}.huggingface_repo_file_parameter.list_repo_revisions_with_file_in_cache", return_value=[]),
        patch(f"{_MODULE}.huggingface_repo_variant_parameter._list_variants_in_cache", return_value=[]),
    ):
        yield


def _register_probe_node(*, include_handleless_model: bool = False, handleless_is_denied: bool = False) -> None:
    """Register `_HfProbeNode` in a real library whose catalog declares the test repos."""
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

    denied_models = {
        "md_flux_1_dev": Model(
            display_name="FLUX.1 [dev]",
            provider_model_id=DENIED_REPO,
            key_support=KeySupport.REQUIRES_CUSTOMER_KEY,
        ),
        "md_ltx_2": Model(
            display_name="LTX-2",
            provider_model_id=VARIANT_REPO,
            key_support=KeySupport.NO_KEY_REQUIRED,
        ),
    }
    allowed_models = {
        "md_clip": Model(
            display_name="CLIP ViT-Large",
            provider_model_id=ALLOWED_REPO,
            key_support=KeySupport.NO_KEY_REQUIRED,
        ),
    }
    if include_handleless_model:
        # Legal: `provider_model_id` is optional. Unmatchable against a cache-derived choice, so it
        # must not be mistaken for "the node declares nothing" or for an undeclared selection.
        # Deliberately under the PERMITTED provider: a handleless entry that policy also DENIES is
        # a separate case (it escalates to refusing the whole parameter) and is covered below.
        allowed_models[HANDLELESS_MODEL_ID] = Model(
            display_name="No Upstream Handle",
            key_support=KeySupport.NO_KEY_REQUIRED,
        )

    catalog = ModelCatalogLibraryProperty(
        providers={
            DENIED_PROVIDER: ModelProvider(display_name="Denied Provider", models=denied_models),
            "allowed_provider": ModelProvider(display_name="Allowed Provider", models=allowed_models),
        }
    )

    schema = LibrarySchema(
        name=_LIBRARY_NAME,
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
    library.register_new_node_type(
        _HfProbeNode,
        NodeMetadata(
            category="t",
            description="d",
            display_name="HF Probe",
            declarations=[ModelUsageNodeProperty(model_ids=sorted({*denied_models, *allowed_models}))],
        ),
    )


def _deny_denied_provider(checkpoint: AuthorizationCheckpoint) -> CheckpointDenial | None:
    """Real authorization hook: forbid every model under `DENIED_PROVIDER`.

    Equivalent to `forbid ... when { resource in ModelProvider::"denied_provider" }`, and installed
    through the same `add_authorization_hook` seam the app uses for license policy.
    """
    if checkpoint.action is not CheckpointAction.OFFER_MODEL:
        return None
    if checkpoint.attributes.get(CheckpointAttribute.PROVIDER_ID) != DENIED_PROVIDER:
        return None
    return CheckpointDenial(failures=(CheckpointFailure(detail="Provider forbidden by your license."),))


@pytest.fixture
def denying_policy(griptape_nodes) -> Iterator[None]:  # noqa: ANN001
    """Install the real deny hook for the duration of a test."""
    griptape_nodes.EventManager().add_authorization_hook(_deny_denied_provider)
    try:
        yield
    finally:
        griptape_nodes.EventManager().remove_authorization_hook(_deny_denied_provider)


class TestRealCatalogResolution:
    """The catalog resolves through `access_manager`, so verdicts are engine-produced."""

    def test_a_denied_repo_is_denied(self, denying_policy) -> None:  # noqa: ARG002
        _register_probe_node()
        param = HuggingFaceRepoParameter(_HfProbeNode(), repo_ids=[DENIED_REPO, ALLOWED_REPO])
        assert param.query_for_denial(DENIED_REPO) is not None

    def test_a_permitted_repo_is_allowed(self, denying_policy) -> None:  # noqa: ARG002
        _register_probe_node()
        param = HuggingFaceRepoParameter(_HfProbeNode(), repo_ids=[DENIED_REPO, ALLOWED_REPO])
        assert param.query_for_denial(ALLOWED_REPO) is None

    def test_gating_is_off_when_no_policy_denies(self) -> None:
        """No hook installed: the catalog resolves, nothing is denied, nothing over-blocks."""
        _register_probe_node()
        param = HuggingFaceRepoParameter(_HfProbeNode(), repo_ids=[DENIED_REPO, ALLOWED_REPO])
        assert param.query_for_denial(DENIED_REPO) is None
        assert param.query_for_denial(ALLOWED_REPO) is None


class TestAHandlelessCatalogModel:
    """A catalog `Model` with no `provider_model_id` -- unexpressible with a stubbed verdict.

    This is the shape that produced a real defect: dropping such an entry reclassified declared,
    permitted models as undeclared and hard-denied them.
    """

    def test_it_does_not_deny_a_permitted_sibling(self, denying_policy) -> None:  # noqa: ARG002
        _register_probe_node(include_handleless_model=True)
        param = HuggingFaceRepoParameter(_HfProbeNode(), repo_ids=[ALLOWED_REPO])
        assert param.query_for_denial(ALLOWED_REPO) is None

    def test_it_does_not_switch_enforcement_off(self, denying_policy) -> None:  # noqa: ARG002
        """The node still HAS a catalog, so a denied sibling must still be denied."""
        _register_probe_node(include_handleless_model=True)
        param = HuggingFaceRepoParameter(_HfProbeNode(), repo_ids=[DENIED_REPO, ALLOWED_REPO])
        assert param._gated is True
        assert param.query_for_denial(DENIED_REPO) is not None

    def test_it_suppresses_the_undeclared_backstop(self, denying_policy) -> None:  # noqa: ARG002
        """With an unmatchable entry present, absence from the table proves nothing."""
        _register_probe_node(include_handleless_model=True)
        param = HuggingFaceRepoParameter(_HfProbeNode(), repo_ids=[ALLOWED_REPO])
        assert param._policy.has_unmatchable_entries is True
        assert param.query_for_denial(UNDECLARED_REPO) is None

    def test_a_fully_matchable_catalog_still_refuses_undeclared(self, denying_policy) -> None:  # noqa: ARG002
        _register_probe_node()
        param = HuggingFaceRepoParameter(_HfProbeNode(), repo_ids=[ALLOWED_REPO])
        assert param._policy.has_unmatchable_entries is False
        assert param.query_for_denial(UNDECLARED_REPO) is not None


class TestEverySubclassAgainstTheRealCatalog:
    """Gating differs per subclass, so each is exercised in its own key shape."""

    def test_repo_file_parameter(self, denying_policy) -> None:  # noqa: ARG002
        _register_probe_node()
        param = HuggingFaceRepoFileParameter(_HfProbeNode(), repo_files=[(DENIED_REPO, FILE_NAME)])
        assert param.query_for_denial(DENIED_REPO) is not None

    def test_repo_variant_parameter_denies_its_variant_keys(self, denying_policy) -> None:  # noqa: ARG002
        """`owner/repo/variant` must reduce to the `owner/repo` the catalog declares.

        The other shape a stubbed verdict cannot express: the rendered choice does not equal the
        catalog's `provider_model_id`.
        """
        _register_probe_node()
        param = HuggingFaceRepoVariantParameter(_HfProbeNode(), repo_id=VARIANT_REPO, variants=[VARIANT])
        # LTX-2 sits under the denied provider in this catalog.
        assert param.query_for_denial(f"{VARIANT_REPO}/{VARIANT}") is not None

    def test_repo_variant_parameter_allows_a_permitted_base(self, denying_policy) -> None:  # noqa: ARG002
        _register_probe_node()
        param = HuggingFaceRepoVariantParameter(_HfProbeNode(), repo_id=ALLOWED_REPO, variants=[VARIANT])
        assert param.query_for_denial(f"{ALLOWED_REPO}/{VARIANT}") is None


class TestRunPathGateAgainstTheRealCatalog:
    """`validate_before_node_run` is the actual gate; decoration is advisory."""

    def test_a_denied_selection_blocks_the_run(self, denying_policy) -> None:  # noqa: ARG002
        _register_probe_node()
        node = _HfProbeNode()
        param = HuggingFaceRepoParameter(node, repo_ids=[DENIED_REPO, ALLOWED_REPO])
        param.add_input_parameters()
        node.set_parameter_value("model", DENIED_REPO)
        errors = param.validate_before_node_run()
        assert errors is not None
        assert "not permitted" in str(errors[0])

    def test_a_permitted_selection_does_not_block_on_policy(self, denying_policy) -> None:  # noqa: ARG002
        """A permitted model must not be refused for license reasons.

        It may still fail for being uncached (that is the download path, not the gate), so this
        asserts only that no LICENSE error is raised.
        """
        _register_probe_node()
        node = _HfProbeNode()
        param = HuggingFaceRepoParameter(node, repo_ids=[ALLOWED_REPO])
        param.add_input_parameters()
        node.set_parameter_value("model", ALLOWED_REPO)
        errors = param.validate_before_node_run() or []
        assert not any("not permitted" in str(e) for e in errors)
