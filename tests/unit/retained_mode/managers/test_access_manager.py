"""Unit tests for `AccessManager`: per-candidate `OFFER_MODEL` authorization queries.

Covers the three request types -- bare, node-attributed, catalog-scoped -- plus
catalog enrichment of checkpoint attributes, the hook chain's recursion-guard
behavior across a per-candidate loop, and failure paths.
"""

from __future__ import annotations

from typing import Any

import pytest

from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes


class _ProbeNode(BaseNode):
    """Concrete `BaseNode` used as the registered node type under test."""

    def __init__(self, name: str, metadata=None) -> None:  # noqa: ANN001
        super().__init__(name=name, metadata=metadata)


class TestAccessManager:
    """Per-candidate `OFFER_MODEL` authorization query manager."""

    _LIBRARY_NAME = "access-manager-test-library"

    @pytest.fixture(autouse=True)
    def _clean_registry(self):  # noqa: ANN202
        from griptape_nodes.node_library.library_registry import LibraryRegistry

        LibraryRegistry._clear()
        yield
        LibraryRegistry._clear()

    def _register(self, node_declarations=(), library_declarations=()):  # noqa: ANN001, ANN202
        from griptape_nodes.node_library.library_registry import (
            LibraryMetadata,
            LibraryRegistry,
            LibrarySchema,
            NodeMetadata,
        )

        schema = LibrarySchema(
            name=self._LIBRARY_NAME,
            library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
            metadata=LibraryMetadata(
                author="t",
                description="d",
                library_version="1.0.0",
                engine_version="1.0.0",
                tags=[],
                declarations=list(library_declarations),
            ),
            categories=[],
            nodes=[],
        )
        library = LibraryRegistry.generate_new_library(library_data=schema)
        library.register_new_node_type(
            _ProbeNode,
            NodeMetadata(category="t", description="d", display_name="Probe", declarations=list(node_declarations)),
        )
        return library

    @staticmethod
    def _catalog():  # noqa: ANN205
        from griptape_nodes.node_library.library_declarations import (
            KeySupport,
            Model,
            ModelCatalogLibraryProperty,
            ModelProvider,
        )

        return ModelCatalogLibraryProperty(
            providers={
                "anthropic": ModelProvider(
                    display_name="Anthropic",
                    models={
                        "gtc_claude_opus_4_7": Model(
                            display_name="Claude Opus 4.7",
                            family="Claude 4",
                            provider_model_id="claude-opus-4-7",
                            key_support=KeySupport.REQUIRES_GRIPTAPE_KEY,
                        ),
                        "gtc_claude_sonnet_4_6": Model(
                            display_name="Claude Sonnet 4.6",
                            family="Claude 4",
                            provider_model_id="claude-sonnet-4-6",
                            key_support=KeySupport.REQUIRES_GRIPTAPE_KEY,
                        ),
                    },
                )
            }
        )

    # ---------- Per-node request ----------

    def test_for_node_unknown_node_type_returns_failure(self, griptape_nodes: GriptapeNodes) -> None:  # noqa: ARG002
        from griptape_nodes.retained_mode.events.access_events import (
            QueryModelAccessForNodeRequest,
            QueryModelAccessForNodeResultFailure,
        )

        result = GriptapeNodes.handle_request(QueryModelAccessForNodeRequest(node_type="Missing"))
        assert isinstance(result, QueryModelAccessForNodeResultFailure)
        # Message is clean prose: names the node type, no KeyError quote/tuple repr.
        details = str(result.result_details)
        assert "Missing" in details
        assert not details.startswith('"')
        assert "('" not in details

    def test_for_node_missing_in_specified_library_returns_clean_failure(
        self,
        griptape_nodes: GriptapeNodes,  # noqa: ARG002
    ) -> None:
        from griptape_nodes.retained_mode.events.access_events import (
            QueryModelAccessForNodeRequest,
            QueryModelAccessForNodeResultFailure,
        )

        self._register()

        result = GriptapeNodes.handle_request(
            QueryModelAccessForNodeRequest(node_type="NotRegistered", specific_library_name=self._LIBRARY_NAME)
        )
        assert isinstance(result, QueryModelAccessForNodeResultFailure)
        # The node-not-found path stringifies a KeyError(library, node_type) tuple; the
        # handler must surface clean prose naming both, not the raw tuple repr.
        details = str(result.result_details)
        assert "NotRegistered" in details
        assert self._LIBRARY_NAME in details
        assert "('" not in details

    def test_for_node_no_hook_all_models_allowed(self, griptape_nodes: GriptapeNodes) -> None:  # noqa: ARG002
        from griptape_nodes.node_library.library_declarations import ModelUsageNodeProperty
        from griptape_nodes.retained_mode.events.access_events import (
            QueryModelAccessForNodeRequest,
            QueryModelAccessForNodeResultSuccess,
        )

        self._register(
            node_declarations=[ModelUsageNodeProperty(model_ids=["gtc_claude_opus_4_7", "gtc_claude_sonnet_4_6"])],
            library_declarations=[self._catalog()],
        )

        result = GriptapeNodes.handle_request(
            QueryModelAccessForNodeRequest(node_type=_ProbeNode.__name__, specific_library_name=self._LIBRARY_NAME)
        )
        assert isinstance(result, QueryModelAccessForNodeResultSuccess)
        assert [v.model_id for v in result.verdicts] == ["gtc_claude_opus_4_7", "gtc_claude_sonnet_4_6"]
        assert all(v.denial is None for v in result.verdicts)

    def test_for_node_hook_denies_one_model_only(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.node_library.library_declarations import ModelUsageNodeProperty
        from griptape_nodes.retained_mode.events.access_events import (
            QueryModelAccessForNodeRequest,
            QueryModelAccessForNodeResultSuccess,
        )
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure

        self._register(
            node_declarations=[ModelUsageNodeProperty(model_ids=["gtc_claude_opus_4_7", "gtc_claude_sonnet_4_6"])],
            library_declarations=[self._catalog()],
        )

        def deny(checkpoint: object) -> CheckpointDenial | None:
            if checkpoint.attributes.get("id") == "gtc_claude_opus_4_7":  # type: ignore[attr-defined]
                return CheckpointDenial(failures=(CheckpointFailure(detail="Opus is not in your plan."),))
            return None

        griptape_nodes.EventManager().add_authorization_hook(deny)
        try:
            result = GriptapeNodes.handle_request(
                QueryModelAccessForNodeRequest(node_type=_ProbeNode.__name__, specific_library_name=self._LIBRARY_NAME)
            )
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(deny)

        assert isinstance(result, QueryModelAccessForNodeResultSuccess)
        denied = [v for v in result.verdicts if v.denial is not None]
        allowed = [v for v in result.verdicts if v.denial is None]
        assert [v.model_id for v in denied] == ["gtc_claude_opus_4_7"]
        assert [v.model_id for v in allowed] == ["gtc_claude_sonnet_4_6"]
        assert denied[0].denial is not None
        assert denied[0].denial.messages() == ["Opus is not in your plan."]

    def test_for_node_verdict_carries_provider_model_id(self, griptape_nodes: GriptapeNodes) -> None:  # noqa: ARG002
        from griptape_nodes.node_library.library_declarations import ModelUsageNodeProperty
        from griptape_nodes.retained_mode.events.access_events import (
            QueryModelAccessForNodeRequest,
            QueryModelAccessForNodeResultSuccess,
        )

        self._register(
            node_declarations=[ModelUsageNodeProperty(model_ids=["gtc_claude_opus_4_7"])],
            library_declarations=[self._catalog()],
        )

        result = GriptapeNodes.handle_request(
            QueryModelAccessForNodeRequest(node_type=_ProbeNode.__name__, specific_library_name=self._LIBRARY_NAME)
        )
        assert isinstance(result, QueryModelAccessForNodeResultSuccess)
        assert len(result.verdicts) == 1
        assert result.verdicts[0].model_id == "gtc_claude_opus_4_7"
        assert result.verdicts[0].provider_model_id == "claude-opus-4-7"

    def test_for_node_unknown_candidate_no_catalog_enrichment(self, griptape_nodes: GriptapeNodes) -> None:
        """Per-node request with an EXPLICIT candidate list including an unknown id.

        Verifies the explicit-override path: the engine asks the hook for the id
        but skips enrichment since the id is not in the catalog.
        """
        from griptape_nodes.retained_mode.events.access_events import (
            QueryModelAccessForNodeRequest,
            QueryModelAccessForNodeResultSuccess,
        )

        self._register(library_declarations=[self._catalog()])

        seen_attributes: list[dict[str, Any]] = []

        def record(checkpoint: object) -> None:
            seen_attributes.append(dict(checkpoint.attributes))  # type: ignore[attr-defined]

        griptape_nodes.EventManager().add_authorization_hook(record)
        try:
            result = GriptapeNodes.handle_request(
                QueryModelAccessForNodeRequest(
                    node_type=_ProbeNode.__name__,
                    candidate_model_ids=["not_in_catalog"],
                    specific_library_name=self._LIBRARY_NAME,
                )
            )
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(record)

        assert isinstance(result, QueryModelAccessForNodeResultSuccess)
        assert result.verdicts[0].model_id == "not_in_catalog"
        assert result.verdicts[0].provider_model_id is None
        assert seen_attributes == [{"node_type": _ProbeNode.__name__, "id": "not_in_catalog"}]

    def test_for_node_per_candidate_loop_does_not_trip_recursion_guard(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.retained_mode.events.access_events import (
            QueryModelAccessForNodeRequest,
            QueryModelAccessForNodeResultSuccess,
        )

        self._register(library_declarations=[self._catalog()])

        seen_model_ids: list[str] = []

        def record(checkpoint: object) -> None:
            seen_model_ids.append(checkpoint.attributes["id"])  # type: ignore[attr-defined]

        griptape_nodes.EventManager().add_authorization_hook(record)
        try:
            result = GriptapeNodes.handle_request(
                QueryModelAccessForNodeRequest(
                    node_type=_ProbeNode.__name__,
                    candidate_model_ids=["gtc_claude_opus_4_7", "gtc_claude_sonnet_4_6", "not_in_catalog"],
                    specific_library_name=self._LIBRARY_NAME,
                )
            )
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(record)

        assert isinstance(result, QueryModelAccessForNodeResultSuccess)
        # Each candidate produces exactly one verdict and one hook call -- the
        # recursion guard does NOT short-circuit subsequent evaluations.
        assert seen_model_ids == ["gtc_claude_opus_4_7", "gtc_claude_sonnet_4_6", "not_in_catalog"]
        assert [v.model_id for v in result.verdicts] == seen_model_ids

    def test_for_node_action_is_offer_model(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.retained_mode.events.access_events import QueryModelAccessForNodeRequest

        self._register(library_declarations=[self._catalog()])

        seen_actions: list[str] = []

        def record(checkpoint: object) -> None:
            seen_actions.append(checkpoint.action)  # type: ignore[attr-defined]

        griptape_nodes.EventManager().add_authorization_hook(record)
        try:
            GriptapeNodes.handle_request(
                QueryModelAccessForNodeRequest(
                    node_type=_ProbeNode.__name__,
                    candidate_model_ids=["gtc_claude_opus_4_7"],
                    specific_library_name=self._LIBRARY_NAME,
                )
            )
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(record)

        assert seen_actions == ["OfferModel"]

    def test_for_node_request_derives_model_ids_from_declarations(self, griptape_nodes: GriptapeNodes) -> None:  # noqa: ARG002
        from griptape_nodes.node_library.library_declarations import ModelUsageNodeProperty
        from griptape_nodes.retained_mode.events.access_events import (
            QueryModelAccessForNodeRequest,
            QueryModelAccessForNodeResultSuccess,
        )

        self._register(
            node_declarations=[ModelUsageNodeProperty(model_ids=["gtc_claude_opus_4_7"])],
            library_declarations=[self._catalog()],
        )

        result = GriptapeNodes.handle_request(
            QueryModelAccessForNodeRequest(node_type=_ProbeNode.__name__, specific_library_name=self._LIBRARY_NAME)
        )
        assert isinstance(result, QueryModelAccessForNodeResultSuccess)
        assert [v.model_id for v in result.verdicts] == ["gtc_claude_opus_4_7"]

    def test_for_node_request_expands_provider_usage(self, griptape_nodes: GriptapeNodes) -> None:  # noqa: ARG002
        from griptape_nodes.node_library.library_declarations import ModelProviderUsageNodeProperty
        from griptape_nodes.retained_mode.events.access_events import (
            QueryModelAccessForNodeRequest,
            QueryModelAccessForNodeResultSuccess,
        )

        self._register(
            node_declarations=[ModelProviderUsageNodeProperty(provider_ids=["anthropic"])],
            library_declarations=[self._catalog()],
        )

        result = GriptapeNodes.handle_request(
            QueryModelAccessForNodeRequest(node_type=_ProbeNode.__name__, specific_library_name=self._LIBRARY_NAME)
        )
        assert isinstance(result, QueryModelAccessForNodeResultSuccess)
        # Whole provider expands to its catalog models in catalog order.
        assert [v.model_id for v in result.verdicts] == ["gtc_claude_opus_4_7", "gtc_claude_sonnet_4_6"]

    def test_for_node_request_with_no_model_declarations_returns_empty_verdicts(
        self,
        griptape_nodes: GriptapeNodes,  # noqa: ARG002
    ) -> None:
        from griptape_nodes.retained_mode.events.access_events import (
            QueryModelAccessForNodeRequest,
            QueryModelAccessForNodeResultSuccess,
        )

        self._register(library_declarations=[self._catalog()])

        result = GriptapeNodes.handle_request(
            QueryModelAccessForNodeRequest(node_type=_ProbeNode.__name__, specific_library_name=self._LIBRARY_NAME)
        )
        assert isinstance(result, QueryModelAccessForNodeResultSuccess)
        assert result.verdicts == []

    def test_for_node_explicit_candidates_override_declarations(self, griptape_nodes: GriptapeNodes) -> None:  # noqa: ARG002
        from griptape_nodes.node_library.library_declarations import ModelUsageNodeProperty
        from griptape_nodes.retained_mode.events.access_events import (
            QueryModelAccessForNodeRequest,
            QueryModelAccessForNodeResultSuccess,
        )

        self._register(
            # Node declares two models, but caller asks about only one of them.
            node_declarations=[ModelUsageNodeProperty(model_ids=["gtc_claude_opus_4_7", "gtc_claude_sonnet_4_6"])],
            library_declarations=[self._catalog()],
        )

        result = GriptapeNodes.handle_request(
            QueryModelAccessForNodeRequest(
                node_type=_ProbeNode.__name__,
                specific_library_name=self._LIBRARY_NAME,
                candidate_model_ids=["gtc_claude_sonnet_4_6"],
            )
        )
        assert isinstance(result, QueryModelAccessForNodeResultSuccess)
        assert [v.model_id for v in result.verdicts] == ["gtc_claude_sonnet_4_6"]

    def test_for_node_preserves_candidate_input_order(self, griptape_nodes: GriptapeNodes) -> None:  # noqa: ARG002
        from griptape_nodes.retained_mode.events.access_events import (
            QueryModelAccessForNodeRequest,
            QueryModelAccessForNodeResultSuccess,
        )

        self._register(library_declarations=[self._catalog()])

        result = GriptapeNodes.handle_request(
            QueryModelAccessForNodeRequest(
                node_type=_ProbeNode.__name__,
                candidate_model_ids=["gtc_claude_sonnet_4_6", "gtc_claude_opus_4_7"],
                specific_library_name=self._LIBRARY_NAME,
            )
        )
        assert isinstance(result, QueryModelAccessForNodeResultSuccess)
        assert [v.model_id for v in result.verdicts] == ["gtc_claude_sonnet_4_6", "gtc_claude_opus_4_7"]

    def test_for_node_catalog_enrichment_attributes_reach_the_hook(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.retained_mode.events.access_events import QueryModelAccessForNodeRequest

        self._register(library_declarations=[self._catalog()])

        seen_attributes: list[dict[str, Any]] = []

        def record(checkpoint: object) -> None:
            seen_attributes.append(dict(checkpoint.attributes))  # type: ignore[attr-defined]

        griptape_nodes.EventManager().add_authorization_hook(record)
        try:
            GriptapeNodes.handle_request(
                QueryModelAccessForNodeRequest(
                    node_type=_ProbeNode.__name__,
                    candidate_model_ids=["gtc_claude_opus_4_7"],
                    specific_library_name=self._LIBRARY_NAME,
                )
            )
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(record)

        assert seen_attributes == [
            {
                "node_type": _ProbeNode.__name__,
                "id": "gtc_claude_opus_4_7",
                "provider_id": "anthropic",
                "model_families": ["Claude 4"],
            }
        ]

    # ---------- Bare request (QueryModelAccessRequest) ----------

    def test_bare_request_no_node_no_catalog_attributes(self, griptape_nodes: GriptapeNodes) -> None:
        """Bare request: hook sees `ID` only, even when a library is registered.

        The bare form must NOT enrich opportunistically -- it has no library scope
        to draw from, so policies operate on the bare model id regardless of what
        catalogs happen to be loaded.
        """
        from griptape_nodes.retained_mode.events.access_events import (
            QueryModelAccessRequest,
            QueryModelAccessResultSuccess,
        )

        # Register a library whose catalog HAS the id we're about to query, just
        # to prove the bare form ignores it.
        self._register(library_declarations=[self._catalog()])

        seen_attributes: list[dict[str, Any]] = []

        def record(checkpoint: object) -> None:
            seen_attributes.append(dict(checkpoint.attributes))  # type: ignore[attr-defined]

        griptape_nodes.EventManager().add_authorization_hook(record)
        try:
            result = GriptapeNodes.handle_request(
                QueryModelAccessRequest(candidate_model_ids=["gtc_claude_opus_4_7", "anything"])
            )
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(record)

        assert isinstance(result, QueryModelAccessResultSuccess)
        # `provider_model_id` is None because the bare form does NOT consult any catalog.
        assert all(v.provider_model_id is None for v in result.verdicts)
        assert seen_attributes == [
            {"id": "gtc_claude_opus_4_7"},
            {"id": "anything"},
        ]

    def test_bare_request_hook_can_deny_on_bare_model_id(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.retained_mode.events.access_events import (
            QueryModelAccessRequest,
            QueryModelAccessResultSuccess,
        )
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure

        def deny(checkpoint: object) -> CheckpointDenial | None:
            if checkpoint.attributes.get("id") == "gtc_claude_opus_4_7":  # type: ignore[attr-defined]
                return CheckpointDenial(failures=(CheckpointFailure(detail="Opus is not in your plan."),))
            return None

        griptape_nodes.EventManager().add_authorization_hook(deny)
        try:
            result = GriptapeNodes.handle_request(
                QueryModelAccessRequest(candidate_model_ids=["gtc_claude_opus_4_7", "gtc_claude_sonnet_4_6"])
            )
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(deny)

        assert isinstance(result, QueryModelAccessResultSuccess)
        denied = [v for v in result.verdicts if v.denial is not None]
        assert [v.model_id for v in denied] == ["gtc_claude_opus_4_7"]

    # ---------- Catalog-scoped request (QueryModelAccessForCatalogRequest) ----------

    def test_for_catalog_enriches_when_id_resolves(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.retained_mode.events.access_events import QueryModelAccessForCatalogRequest

        self._register(library_declarations=[self._catalog()])

        seen_attributes: list[dict[str, Any]] = []

        def record(checkpoint: object) -> None:
            seen_attributes.append(dict(checkpoint.attributes))  # type: ignore[attr-defined]

        griptape_nodes.EventManager().add_authorization_hook(record)
        try:
            GriptapeNodes.handle_request(
                QueryModelAccessForCatalogRequest(
                    library_name=self._LIBRARY_NAME,
                    candidate_model_ids=["gtc_claude_opus_4_7"],
                )
            )
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(record)

        # No "node_type" attribute -- catalog-scoped form has no node attribution.
        assert seen_attributes == [
            {
                "id": "gtc_claude_opus_4_7",
                "provider_id": "anthropic",
                "model_families": ["Claude 4"],
            }
        ]

    def test_for_catalog_unknown_library_returns_success_with_bare_verdicts(
        self, griptape_nodes: GriptapeNodes
    ) -> None:
        from griptape_nodes.retained_mode.events.access_events import (
            QueryModelAccessForCatalogRequest,
            QueryModelAccessForCatalogResultSuccess,
        )

        # No library registered at this name.
        seen_attributes: list[dict[str, Any]] = []

        def record(checkpoint: object) -> None:
            seen_attributes.append(dict(checkpoint.attributes))  # type: ignore[attr-defined]

        griptape_nodes.EventManager().add_authorization_hook(record)
        try:
            result = GriptapeNodes.handle_request(
                QueryModelAccessForCatalogRequest(
                    library_name="library-that-does-not-exist",
                    candidate_model_ids=["gtc_claude_opus_4_7"],
                )
            )
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(record)

        assert isinstance(result, QueryModelAccessForCatalogResultSuccess)
        assert result.verdicts[0].model_id == "gtc_claude_opus_4_7"
        assert result.verdicts[0].provider_model_id is None
        # Hook still got the bare model_id, no enrichment.
        assert seen_attributes == [{"id": "gtc_claude_opus_4_7"}]

    def test_for_catalog_unknown_id_no_enrichment_but_query_still_happens(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.retained_mode.events.access_events import (
            QueryModelAccessForCatalogRequest,
            QueryModelAccessForCatalogResultSuccess,
        )

        self._register(library_declarations=[self._catalog()])

        seen_attributes: list[dict[str, Any]] = []

        def record(checkpoint: object) -> None:
            seen_attributes.append(dict(checkpoint.attributes))  # type: ignore[attr-defined]

        griptape_nodes.EventManager().add_authorization_hook(record)
        try:
            result = GriptapeNodes.handle_request(
                QueryModelAccessForCatalogRequest(
                    library_name=self._LIBRARY_NAME,
                    candidate_model_ids=["not_in_catalog"],
                )
            )
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(record)

        assert isinstance(result, QueryModelAccessForCatalogResultSuccess)
        assert result.verdicts[0].model_id == "not_in_catalog"
        assert result.verdicts[0].provider_model_id is None
        assert seen_attributes == [{"id": "not_in_catalog"}]


class TestCodecAccess:
    """`QueryCodecAccessRequest` mirrors `QueryModelAccessRequest` for codec dropdowns.

    Direction (`"read"` / `"write"`) picks the checkpoint action so a codec
    that is legal to read but not legal to encode gets different verdicts
    for the same codec name. An unknown direction is a caller bug and is
    surfaced via a Failure result, not silently defaulted.
    """

    def test_read_direction_uses_read_video_codec_action(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.retained_mode.events.access_events import (
            CodecAccessDirection,
            QueryCodecAccessRequest,
            QueryCodecAccessResultSuccess,
        )

        seen_actions: list[str] = []

        def record(checkpoint: object) -> None:
            seen_actions.append(checkpoint.action)  # type: ignore[attr-defined]

        griptape_nodes.EventManager().add_authorization_hook(record)
        try:
            result = GriptapeNodes.handle_request(
                QueryCodecAccessRequest(candidate_codecs=["h264"], direction=CodecAccessDirection.READ)
            )
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(record)

        assert isinstance(result, QueryCodecAccessResultSuccess)
        assert seen_actions == ["ReadVideoCodec"]

    def test_write_direction_uses_write_video_codec_action(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.retained_mode.events.access_events import (
            CodecAccessDirection,
            QueryCodecAccessRequest,
            QueryCodecAccessResultSuccess,
        )

        seen_actions: list[str] = []

        def record(checkpoint: object) -> None:
            seen_actions.append(checkpoint.action)  # type: ignore[attr-defined]

        griptape_nodes.EventManager().add_authorization_hook(record)
        try:
            result = GriptapeNodes.handle_request(
                QueryCodecAccessRequest(candidate_codecs=["hevc"], direction=CodecAccessDirection.WRITE)
            )
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(record)

        assert isinstance(result, QueryCodecAccessResultSuccess)
        assert seen_actions == ["WriteVideoCodec"]

    def test_unknown_direction_returns_failure(self, griptape_nodes: GriptapeNodes) -> None:  # noqa: ARG002
        # Exercises the runtime `case _:` guard for wire-side stray values --
        # ``direction`` is a StrEnum so pyright catches Python-side typos, but
        # a request deserialized from the WS boundary can still carry any
        # string. The handler must surface a clean Failure result rather than
        # crash on the unhandled case. ``# type: ignore`` is intentional here.
        from griptape_nodes.retained_mode.events.access_events import (
            QueryCodecAccessRequest,
            QueryCodecAccessResultFailure,
        )

        result = GriptapeNodes.handle_request(
            QueryCodecAccessRequest(candidate_codecs=["h264"], direction="rewrite")  # type: ignore[arg-type]
        )

        assert isinstance(result, QueryCodecAccessResultFailure)
        details = str(result.result_details)
        assert "'rewrite'" in details or "rewrite" in details
        assert "read" in details.lower()
        assert "write" in details.lower()

    def test_container_format_threaded_through_checkpoint_attributes(self, griptape_nodes: GriptapeNodes) -> None:
        # A policy that only cares about codec+container pairing (e.g. hevc in
        # mp4 is denied but hevc in mkv is allowed) needs the container to
        # appear on the checkpoint attributes; the request field carries it.
        from griptape_nodes.retained_mode.events.access_events import (
            CodecAccessDirection,
            QueryCodecAccessRequest,
            QueryCodecAccessResultSuccess,
        )

        seen_attributes: list[dict[str, Any]] = []

        def record(checkpoint: object) -> None:
            seen_attributes.append(dict(checkpoint.attributes))  # type: ignore[attr-defined]

        griptape_nodes.EventManager().add_authorization_hook(record)
        try:
            result = GriptapeNodes.handle_request(
                QueryCodecAccessRequest(
                    candidate_codecs=["hevc"], direction=CodecAccessDirection.WRITE, container_format="mp4"
                )
            )
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(record)

        assert isinstance(result, QueryCodecAccessResultSuccess)
        assert seen_attributes == [{"id": "hevc", "container_format": "mp4"}]

    def test_container_format_omitted_when_none(self, griptape_nodes: GriptapeNodes) -> None:
        # When the caller doesn't scope the query to a container, the attribute
        # is not populated -- a policy that requires the pairing key still
        # denies deterministically (missing key), not on a synthesized default.
        from griptape_nodes.retained_mode.events.access_events import (
            CodecAccessDirection,
            QueryCodecAccessRequest,
        )

        seen_attributes: list[dict[str, Any]] = []

        def record(checkpoint: object) -> None:
            seen_attributes.append(dict(checkpoint.attributes))  # type: ignore[attr-defined]

        griptape_nodes.EventManager().add_authorization_hook(record)
        try:
            GriptapeNodes.handle_request(
                QueryCodecAccessRequest(candidate_codecs=["h264"], direction=CodecAccessDirection.READ)
            )
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(record)

        assert seen_attributes == [{"id": "h264"}]

    def test_denied_verdict_carries_denial(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.retained_mode.events.access_events import (
            CodecAccessDirection,
            QueryCodecAccessRequest,
            QueryCodecAccessResultSuccess,
        )
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure

        def deny_hevc(checkpoint: object) -> CheckpointDenial | None:
            if checkpoint.attributes.get("id") == "hevc":  # type: ignore[attr-defined]
                return CheckpointDenial(failures=(CheckpointFailure(detail="HEVC write is not in your plan."),))
            return None

        griptape_nodes.EventManager().add_authorization_hook(deny_hevc)
        try:
            result = GriptapeNodes.handle_request(
                QueryCodecAccessRequest(
                    candidate_codecs=["h264", "hevc", "vp9"],
                    direction=CodecAccessDirection.WRITE,
                    container_format="mp4",
                )
            )
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(deny_hevc)

        assert isinstance(result, QueryCodecAccessResultSuccess)
        assert [v.codec for v in result.verdicts] == ["h264", "hevc", "vp9"]
        assert [v.denial is None for v in result.verdicts] == [True, False, True]
        # Each verdict carries the container it was queried with, so callers
        # rendering a per-container dropdown don't have to re-associate.
        assert all(v.container_format == "mp4" for v in result.verdicts)

    def test_id_attribute_parity_between_query_and_enforcement(
        self, griptape_nodes: GriptapeNodes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A single hook keying on attributes["id"] must deny both paths for the same codec.

        Regression for the fall-open bug caught in the initial PR review: the
        enforcement checkpoint originally set only ``CONTAINER_FORMAT`` on
        ``attributes`` while the query path set ``ID``. A hook written to the
        model-access convention (`attributes["id"]`) would deny the dropdown
        but silently allow the actual read/write. Both paths must now surface
        identical facts to the hook chain.
        """
        from griptape_nodes.retained_mode.events.access_events import (
            CodecAccessDirection,
            QueryCodecAccessRequest,
            QueryCodecAccessResultSuccess,
        )
        from griptape_nodes.retained_mode.managers.artifact_providers.video.video_artifact_provider import (
            VideoArtifactProvider,
        )
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure

        # Hook keys on attributes["id"], the same convention used by every
        # model-access hook and by our own test-hook examples.
        seen_attributes: list[dict[str, Any]] = []

        def deny_hevc_by_id(checkpoint: object) -> CheckpointDenial | None:
            seen_attributes.append(dict(checkpoint.attributes))  # type: ignore[attr-defined]
            if checkpoint.attributes.get("id") == "hevc":  # type: ignore[attr-defined]
                return CheckpointDenial(failures=(CheckpointFailure(detail="hevc is not licensed."),))
            return None

        # --- Query path: QueryCodecAccessRequest ---
        griptape_nodes.EventManager().add_authorization_hook(deny_hevc_by_id)
        try:
            query_result = GriptapeNodes.handle_request(
                QueryCodecAccessRequest(
                    candidate_codecs=["hevc"], direction=CodecAccessDirection.WRITE, container_format="mp4"
                )
            )
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(deny_hevc_by_id)

        assert isinstance(query_result, QueryCodecAccessResultSuccess)
        assert query_result.verdicts[0].denial is not None
        query_attrs = seen_attributes[-1]

        # --- Enforcement path: VideoArtifactProvider.check_write_permission ---
        # Monkeypatch ffprobe so we can inject a canned codec without shelling
        # out. The read-side path is functionally identical (both go through
        # ``_evaluate_codec_checkpoint``), so exercising the write side is
        # sufficient to prove the enforcement checkpoint carries ``id``.
        canned_probe = {"streams": [{"codec_type": "video", "codec_name": "hevc"}]}
        monkeypatch.setattr(VideoArtifactProvider, "_run_ffprobe", classmethod(lambda cls, source_path: canned_probe))  # noqa: ARG005

        provider = VideoArtifactProvider(registry=None)  # type: ignore[arg-type]

        seen_attributes.clear()
        griptape_nodes.EventManager().add_authorization_hook(deny_hevc_by_id)
        try:
            enforcement_denial = provider.check_write_permission(b"fake mp4 bytes", "mp4", file_name="clip.mp4")
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(deny_hevc_by_id)

        # The critical assertion: the enforcement path denies for the SAME
        # hook shape that the query path denies for.
        assert enforcement_denial is not None
        enforcement_attrs = seen_attributes[-1]

        # Both paths must present the codec via attributes["id"] so any hook
        # written to the model-access convention behaves identically on both.
        assert query_attrs.get("id") == "hevc"
        assert enforcement_attrs.get("id") == "hevc"
        # Enforcement also carries the container it inferred from the sniff;
        # query carries the container the caller supplied.
        assert query_attrs.get("container_format") == "mp4"
        assert enforcement_attrs.get("container_format") == "mp4"
