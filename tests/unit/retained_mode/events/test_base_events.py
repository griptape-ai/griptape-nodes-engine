"""Tests for RequestPayload base class broadcast_result behavior."""

import importlib
import logging
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any
from unittest.mock import Mock

import pytest
from griptape.artifacts import AudioArtifact, BlobArtifact, ImageArtifact

from griptape_nodes.retained_mode.events.base_events import (
    _BLOB_ARTIFACT_TYPE_NAMES,
    BLOB_FIELD_METADATA_KEY,
    EventResultFailure,
    EventResultSuccess,
    ExecutionEvent,
    RequestPayload,
    ResultDetail,
    ResultDetails,
    ResultPayloadSuccess,
    StrictModeViolationDetail,
    _blank_oversized_blobs,
    _blank_oversized_tagged_blob_fields,
    _max_blob_artifact_b64_bytes,
    _warn_blanked,
)
from griptape_nodes.retained_mode.events.connection_events import ListConnectionsForNodeResultSuccess
from griptape_nodes.retained_mode.events.event_converter import converter, safe_unstructure
from griptape_nodes.retained_mode.events.execution_events import (
    ControlFlowResolvedEvent,
    ExecuteNodeRequest,
    ExecuteNodeResultSuccess,
    GriptapeEvent,
    NodeResolvedEvent,
    ParameterValueUpdateEvent,
)
from griptape_nodes.retained_mode.events.node_events import GetAllNodeInfoResultSuccess
from griptape_nodes.retained_mode.events.os_events import ReadFileRequest
from griptape_nodes.retained_mode.events.parameter_events import (
    AlterElementEvent,
    GetNodeElementDetailsResultSuccess,
    GetParameterValueResultSuccess,
    OnParameterValueChanged,
    SetParameterValueRequest,
    SetParameterValueResultSuccess,
)
from griptape_nodes.retained_mode.events.path_filter import apply_path_tree, build_path_tree
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry
from griptape_nodes.retained_mode.events.project_events import (
    GetAllSituationsForProjectRequest,
    GetAllSituationsForProjectResultFailure,
    GetAllSituationsForProjectResultSuccess,
)
from griptape_nodes.retained_mode.managers.config_manager import ConfigManager
from griptape_nodes.retained_mode.managers.settings import (
    DEFAULT_MAX_BLOB_ARTIFACT_B64_BYTES,
    MAX_BLOB_ARTIFACT_B64_BYTES_KEY,
)


class TestBroadcastResultDefaults:
    def test_request_payload_broadcasts_by_default(self) -> None:
        """All RequestPayload subclasses broadcast results unless they opt out."""
        assert RequestPayload.broadcast_result is True

    def test_read_file_request_does_not_broadcast_by_default(self) -> None:
        """ReadFileRequest opts out of broadcasting to avoid sending large payloads."""
        assert ReadFileRequest.broadcast_result is False

    def test_broadcast_result_false_accessible_on_instance(self) -> None:
        """broadcast_result is accessible on instances as well as the class."""
        request = ReadFileRequest()
        assert request.broadcast_result is False


class TestResultDetailsRoundTrip:
    """Regression: cattrs must preserve ResultDetail subclass identity.

    Before include_subclasses(ResultDetail, converter), structuring a
    list[ResultDetail] coerced every entry to the base class, dropping
    StrictModeViolationDetail's extra fields silently. Wire-format
    consumers then could not distinguish a strict-mode violation from a
    plain detail.
    """

    def test_strict_mode_violation_detail_survives_roundtrip(self) -> None:
        violation = StrictModeViolationDetail(
            level=logging.WARNING,
            message="bad node",
            rule_id="rule-x",
            severity="warning",
            subject="node-1",
            library_name="libA",
        )
        original = ResultDetails(violation)

        data = converter.unstructure(original)
        restored = converter.structure(data, ResultDetails)

        roundtrip = restored.result_details[0]
        assert isinstance(roundtrip, StrictModeViolationDetail)
        assert roundtrip.rule_id == "rule-x"
        assert roundtrip.severity == "warning"
        assert roundtrip.subject == "node-1"
        assert roundtrip.library_name == "libA"
        assert roundtrip.level == logging.WARNING
        assert roundtrip.message == "bad node"

    def test_plain_result_detail_still_roundtrips(self) -> None:
        plain = ResultDetail(level=logging.INFO, message="hello")
        original = ResultDetails(plain)

        data = converter.unstructure(original)
        restored = converter.structure(data, ResultDetails)

        roundtrip = restored.result_details[0]
        assert type(roundtrip) is ResultDetail
        assert roundtrip.level == logging.INFO
        assert roundtrip.message == "hello"

    def test_mixed_list_preserves_each_subclass(self) -> None:
        plain = ResultDetail(level=logging.INFO, message="info-line")
        violation = StrictModeViolationDetail(
            level=logging.ERROR,
            message="boom",
            rule_id="r2",
            severity="error",
            subject="node-2",
            library_name=None,
        )
        original = ResultDetails(plain, violation)

        data = converter.unstructure(original)
        restored = converter.structure(data, ResultDetails)

        first, second = restored.result_details
        assert type(first) is ResultDetail
        assert isinstance(second, StrictModeViolationDetail)
        assert second.rule_id == "r2"


class TestBuildPathTree:
    def test_single_key(self) -> None:
        assert build_path_tree(["a"]) == {"a": {}}

    def test_nested_path(self) -> None:
        assert build_path_tree(["a.b"]) == {"a": {"b": {}}}

    def test_shared_prefix_merged(self) -> None:
        assert build_path_tree(["a.b", "a.c"]) == {"a": {"b": {}, "c": {}}}

    def test_deep_path(self) -> None:
        assert build_path_tree(["a.b.c.d"]) == {"a": {"b": {"c": {"d": {}}}}}

    def test_wildcard_path(self) -> None:
        assert build_path_tree(["a.*.b"]) == {"a": {"*": {"b": {}}}}

    def test_empty_list(self) -> None:
        assert build_path_tree([]) == {}

    def test_prefix_wins_broad_first(self) -> None:
        # "a" (keep-whole) added before "a.b" (narrow) — broad wins.
        assert build_path_tree(["a", "a.b"]) == {"a": {}}

    def test_prefix_wins_narrow_first(self) -> None:
        # "a.b" added before "a" — broad still wins by overwriting the subtree.
        assert build_path_tree(["a.b", "a"]) == {"a": {}}

    def test_prefix_wins_deep(self) -> None:
        # "a.b" dominates "a.b.c" regardless of order.
        assert build_path_tree(["a.b.c", "a.b"]) == {"a": {"b": {}}}
        assert build_path_tree(["a.b", "a.b.c"]) == {"a": {"b": {}}}


class TestApplyPathTree:
    def test_non_dict_scalar_passthrough(self) -> None:
        tree = {"a": {}}
        assert apply_path_tree("hello", tree) == "hello"
        assert apply_path_tree("hello", tree) is not None  # non-dict always passes through
        assert apply_path_tree(None, tree) is None

    def test_named_field_kept(self) -> None:
        assert apply_path_tree({"a": 1, "b": 2}, {"a": {}}) == {"a": 1}

    def test_unmatched_key_returns_empty_and_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            result = apply_path_tree({"a": 1}, {"z": {}})
        assert result == {}
        assert "z" in caplog.text

    def test_nested_unmatched_key_warns_with_full_path(self, caplog: pytest.LogCaptureFixture) -> None:
        # "situations.nme" — top-level key exists but nested key is a typo
        data = {"situations": {"hello": "world"}}
        with caplog.at_level(logging.WARNING):
            apply_path_tree(data, build_path_tree(["situations.nme"]))
        assert "situations.nme" in caplog.text

    def test_empty_tree_returns_empty(self) -> None:
        assert apply_path_tree({"a": 1, "b": 2}, {}) == {}

    def test_nested_field(self) -> None:
        data = {"a": {"b": 1, "c": 2}, "x": 9}
        assert apply_path_tree(data, {"a": {"b": {}}}) == {"a": {"b": 1}}

    def test_list_traversal(self) -> None:
        data = {"items": [{"name": "x", "size": 10}, {"name": "y", "size": 20}]}
        assert apply_path_tree(data, {"items": {"name": {}}}) == {"items": [{"name": "x"}, {"name": "y"}]}

    def test_list_non_dict_items_pass_through(self) -> None:
        data = {"tags": ["a", "b", "c"]}
        assert apply_path_tree(data, {"tags": {"x": {}}}) == {"tags": ["a", "b", "c"]}

    def test_wildcard_dict_of_objects(self) -> None:
        data = {
            "workflows": {
                "/path/a": {"name": "Foo", "schema": {"big": "data"}},
                "/path/b": {"name": "Bar", "schema": {"big": "data"}},
            }
        }
        result = apply_path_tree(data, build_path_tree(["workflows.*.name"]))
        assert result == {"workflows": {"/path/a": {"name": "Foo"}, "/path/b": {"name": "Bar"}}}

    def test_wildcard_leaf_keeps_whole_value(self) -> None:
        data = {"workflows": {"/path/a": {"name": "Foo", "schema": {"big": "data"}}}}
        result = apply_path_tree(data, build_path_tree(["workflows.*"]))
        assert result == {"workflows": {"/path/a": {"name": "Foo", "schema": {"big": "data"}}}}

    def test_wildcard_with_named_siblings_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        data = {"a": {"x": 1}, "b": {"x": 2}}
        with caplog.at_level(logging.WARNING):
            apply_path_tree(data, {"*": {"x": {}}, "meta": {}})
        assert "named keys" in caplog.text
        assert "meta" in caplog.text


class TestApplyPathTreeWarningDedup:
    def test_wildcard_typo_warns_once_with_star_path(self, caplog: pytest.LogCaptureFixture) -> None:
        # One typo under a wildcard across many values must dedupe to a single warning
        # reported with the "*" spelling, not one warning per resolved key.
        data = {"workflows": {f"/path/{i}": {"name": str(i)} for i in range(50)}}
        with caplog.at_level(logging.WARNING):
            apply_path_tree(data, build_path_tree(["workflows.*.nme"]))
        matching = [r for r in caplog.records if "not found" in r.message]
        assert len(matching) == 1
        assert "workflows.*.nme" in matching[0].message

    def test_list_typo_warns_once(self, caplog: pytest.LogCaptureFixture) -> None:
        data = {"items": [{"name": "x"}, {"name": "y"}, {"name": "z"}]}
        with caplog.at_level(logging.WARNING):
            apply_path_tree(data, build_path_tree(["items.nme"]))
        matching = [r for r in caplog.records if "not found" in r.message]
        assert len(matching) == 1
        assert "items.nme" in matching[0].message


class TestEventResultDictFiltering:
    """Integration coverage for EventResult.dict()'s fields filtering.

    The path-projection helpers are unit-tested above; these exercise the wire logic
    that wraps them: the succeeded() gate, framework-field re-add, and [] vs None.
    """

    def _success(self, fields: list[str] | None) -> dict:
        request = GetAllSituationsForProjectRequest(fields=fields)
        result = GetAllSituationsForProjectResultSuccess(
            situations={"a": "macro"}, descriptions={"a": "desc"}, result_details="ok"
        )
        return EventResultSuccess(request=request, result=result).dict()["result"]

    def test_none_returns_all_fields(self) -> None:
        keys = self._success(fields=None)
        assert set(keys) == {"situations", "descriptions", "result_details", "altered_workflow_state"}

    def test_empty_list_returns_only_framework_fields(self) -> None:
        keys = self._success(fields=[])
        assert set(keys) == {"result_details", "altered_workflow_state"}

    def test_selected_field_plus_framework_fields(self) -> None:
        result = self._success(fields=["situations"])
        assert set(result) == {"situations", "result_details", "altered_workflow_state"}
        assert result["situations"] == {"a": "macro"}

    def test_failure_result_is_never_filtered(self) -> None:
        # A success-shaped fields filter on a failed request must not strip the exception.
        request = GetAllSituationsForProjectRequest(fields=["situations"])
        result = GetAllSituationsForProjectResultFailure(result_details="boom", exception=ValueError("kaboom"))
        payload = EventResultFailure(request=request, result=result).dict()["result"]
        assert payload["exception"]["message"] == "kaboom"
        assert "result_details" in payload

    def test_bogus_top_level_path_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            self._success(fields=["situatons"])  # typo
        assert any("situatons" in r.message and "not found" in r.message for r in caplog.records)


# Single source of truth for blob-carrying payloads. Each entry maps a payload type to:
#   - a factory that builds an instance with the given blob artifact in its tagged field, and
#   - the key-path from the serialized payload down to that artifact's base64 ``value``.
_BLOB_CASE_BUILDERS: dict[type, tuple[Callable[[Any], Any], tuple[str, ...]]] = {
    NodeResolvedEvent: (
        lambda a: NodeResolvedEvent(node_name="n", parameter_output_values={"out": a}, node_type="T"),
        ("parameter_output_values", "out", "value"),
    ),
    ParameterValueUpdateEvent: (
        lambda a: ParameterValueUpdateEvent(node_name="n", parameter_name="p", data_type="ImageArtifact", value=a),
        ("value", "value"),
    ),
    ControlFlowResolvedEvent: (
        lambda a: ControlFlowResolvedEvent(end_node_name="n", parameter_output_values={"out": a}),
        ("parameter_output_values", "out", "value"),
    ),
    GriptapeEvent: (
        lambda a: GriptapeEvent(node_name="n", parameter_name="p", type="event", value=a),
        ("value", "value"),
    ),
    AlterElementEvent: (
        lambda a: AlterElementEvent(element_details={"node_name": "n", "value": a}),
        ("element_details", "value", "value"),
    ),
    SetParameterValueResultSuccess: (
        lambda a: SetParameterValueResultSuccess(finalized_value=a, data_type="ImageArtifact", result_details="ok"),
        ("finalized_value", "value"),
    ),
    GetParameterValueResultSuccess: (
        lambda a: GetParameterValueResultSuccess(
            input_types=["ImageArtifact"],
            type="ImageArtifact",
            output_type="ImageArtifact",
            value=a,
            result_details="ok",
        ),
        ("value", "value"),
    ),
    GetNodeElementDetailsResultSuccess: (
        lambda a: GetNodeElementDetailsResultSuccess(element_details={"value": a}, result_details="ok"),
        ("element_details", "value", "value"),
    ),
    GetAllNodeInfoResultSuccess: (
        lambda a: GetAllNodeInfoResultSuccess(
            metadata={},
            node_resolution_state="",
            locked=False,
            connections=ListConnectionsForNodeResultSuccess(
                incoming_connections=[], outgoing_connections=[], result_details="ok"
            ),
            element_id_to_value={"e1": a},
            root_node_element={},
            result_details="ok",
        ),
        ("element_id_to_value", "e1", "value"),
    ),
    ExecuteNodeResultSuccess: (
        lambda a: ExecuteNodeResultSuccess(parameter_output_values={"out": a}, result_details="ok"),
        ("parameter_output_values", "out", "value"),
    ),
    OnParameterValueChanged: (
        lambda a: OnParameterValueChanged(
            node_name="n", parameter_name="p", data_type="ImageArtifact", value=a, result_details="ok"
        ),
        ("value", "value"),
    ),
    # Requests carry blobs too: the result echoes the request back to clients, so its value-bearing
    # fields are tagged and stripped in EventResult.dict() (not EventRequest.dict()).
    SetParameterValueRequest: (
        lambda a: SetParameterValueRequest(parameter_name="p", value=a, node_name="n"),
        ("value", "value"),
    ),
    ExecuteNodeRequest: (
        lambda a: ExecuteNodeRequest(node_name="n", parameter_values={"p": a}),
        ("parameter_values", "p", "value"),
    ),
}
# Per-case parametrize ids (payload type names, in dict order).
_BLOB_TAGGED_FIELD_IDS = [payload_type.__name__ for payload_type in _BLOB_CASE_BUILDERS]


@dataclass(kw_only=True)
class _TaggedResult(ResultPayloadSuccess):
    """A result with one blob-tagged field and one untagged field, for _strip_tagged_blob_fields tests."""

    tagged: Any = field(default=None, metadata={BLOB_FIELD_METADATA_KEY: True})
    untagged: Any = None


@dataclass(kw_only=True)
class _UntaggedResult(ResultPayloadSuccess):
    """A result with no blob-tagged fields (nothing to strip)."""

    plain: Any = None


class TestBlankOversizedBlobs:
    """The in-place walker over a freshly-serialized structure."""

    def test_over_threshold_blanked_in_place(self) -> None:
        for type_name in _BLOB_ARTIFACT_TYPE_NAMES:
            artifact = {"type": type_name, "value": "A" * 200, "format": "png"}
            blanked = _blank_oversized_blobs(artifact, max_b64_bytes=100)
            assert blanked == [(type_name, 200)]
            assert artifact["value"] is None
            assert artifact["type"] == type_name  # wrapper + metadata kept
            assert artifact["format"] == "png"

    def test_under_threshold_preserved(self) -> None:
        artifact = _blob_artifact(50)
        assert _blank_oversized_blobs(artifact, max_b64_bytes=100) == []
        assert artifact["value"] == "A" * 50

    def test_non_blob_and_plain_string_untouched(self) -> None:
        url = {"type": "ImageUrlArtifact", "value": "http://x/" + "a" * 500}
        text = {"type": "TextArtifact", "value": "z" * 500}
        plain = {"some_field": "x" * 1000}
        for obj in (url, text, plain):
            assert _blank_oversized_blobs(obj, max_b64_bytes=100) == []
        assert url["value"].startswith("http://")
        assert text["value"] == "z" * 500

    def test_nested_dicts_and_lists_walked(self) -> None:
        payload = {"e1": _blob_artifact(300), "e2": _blob_artifact(10), "nested": {"deep": [_blob_artifact(300)]}}
        blanked = _blank_oversized_blobs(payload, max_b64_bytes=100)
        assert sorted(blanked) == [("ImageArtifact", 300), ("ImageArtifact", 300)]
        assert payload["e1"]["value"] is None
        assert payload["e2"]["value"] == "A" * 10
        assert payload["nested"]["deep"][0]["value"] is None

    def test_non_string_value_ignored(self) -> None:
        artifact = {"type": "ImageArtifact", "value": None}
        assert _blank_oversized_blobs(artifact, max_b64_bytes=100) == []


class TestStripTaggedBlobFields:
    """Only fields carrying the blob tag are walked."""

    def test_only_tagged_fields_are_walked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = _TaggedResult(tagged=None, untagged=None, result_details="ok")
        serialized = {"tagged": _blob_artifact(300), "untagged": _blob_artifact(300)}
        _mock_blob_threshold(monkeypatch, 100)

        _blank_oversized_tagged_blob_fields(payload, serialized)

        assert serialized["tagged"]["value"] is None
        assert serialized["untagged"]["value"] == "A" * 300  # untagged field never inspected

    def test_payload_without_tagged_fields_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No blob-tagged fields -> return before even reading the threshold.
        threshold = Mock()
        monkeypatch.setattr("griptape_nodes.retained_mode.events.base_events._max_blob_artifact_b64_bytes", threshold)
        _blank_oversized_tagged_blob_fields(_UntaggedResult(plain="x", result_details="ok"), {"plain": "A" * 500})
        threshold.assert_not_called()

    def test_non_dataclass_payload_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Defensive guard: a non-dataclass, or a dataclass *class* rather than an instance,
        # must be ignored (no field introspection, no mutation) rather than crash serialization.
        threshold = Mock()
        monkeypatch.setattr("griptape_nodes.retained_mode.events.base_events._max_blob_artifact_b64_bytes", threshold)
        serialized = {"tagged": _blob_artifact(500)}

        not_a_dataclass: Any = object()
        _blank_oversized_tagged_blob_fields(not_a_dataclass, serialized)

        dataclass_type: Any = _TaggedResult  # the class object, not an instance
        _blank_oversized_tagged_blob_fields(dataclass_type, serialized)

        assert serialized["tagged"]["value"] == "A" * 500  # untouched
        threshold.assert_not_called()

    def test_warning_names_the_node_when_payload_exposes_it(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # payload.node_name (best-effort) flows into the warning to point users at the offending node.
        _mock_blob_threshold(monkeypatch, 100)
        payload = NodeResolvedEvent(node_name="MyNode", parameter_output_values={}, node_type="T")
        with caplog.at_level(logging.WARNING):
            _blank_oversized_tagged_blob_fields(payload, {"parameter_output_values": {"out": _blob_artifact(300)}})
        assert "MyNode" in caplog.records[-1].getMessage()


class TestMaxBlobArtifactB64Bytes:
    def test_reads_setting_with_documented_key_default_and_cast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config_manager = _mock_blob_threshold(monkeypatch, 4242)
        assert _max_blob_artifact_b64_bytes() == 4242  # noqa: PLR2004
        config_manager.get_config_value.assert_called_once_with(
            MAX_BLOB_ARTIFACT_B64_BYTES_KEY, default=DEFAULT_MAX_BLOB_ARTIFACT_B64_BYTES, cast_type=int
        )


class TestWarnBlanked:
    """A WARNING is logged for every blanking (not deduped); the node name appears when known."""

    def test_warns_each_call_with_node_and_details(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            _warn_blanked("SetParameterValueResultSuccess", "GenerateImage", [("ImageArtifact", 500)], 100)
            _warn_blanked("NodeResolvedEvent", "LoadAudio", [("AudioArtifact", 900)], 100)
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warns) == 2  # noqa: PLR2004 -- one per blanking, not deduped
        first = warns[0].getMessage()
        assert "GenerateImage" in first
        assert "ImageArtifact" in first
        assert "500" in first
        assert "max_blob_artifact_b64_bytes" in first
        assert "LoadAudio" in warns[1].getMessage()

    def test_omits_node_clause_when_unknown(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            _warn_blanked("GetAllNodeInfoResultSuccess", None, [("ImageArtifact", 500)], 100)
        message = caplog.records[-1].getMessage()
        assert "node '" not in message  # no node clause when node_name is None
        assert "GetAllNodeInfoResultSuccess" in message


class TestSerializationBlanksBlobs:
    """The integration: .dict() blanks oversized blob-tagged fields on the wire form only."""

    def test_event_result_dict_blanks_but_leaves_in_memory_result_full(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_blob_threshold(monkeypatch, 100)
        image = ImageArtifact(value=b"\x00" * 300, format="png", width=1, height=1)
        result = SetParameterValueResultSuccess(finalized_value=image, data_type="ImageArtifact", result_details="ok")
        event = EventResultSuccess(
            request=SetParameterValueRequest(parameter_name="p", node_name="n", value=None), result=result
        )

        wire = event.dict()

        assert wire["result"]["finalized_value"]["value"] is None  # blanked on the wire
        assert result.finalized_value is image  # in-memory object untouched
        assert image.value == b"\x00" * 300

    def test_event_result_dict_preserves_under_threshold(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_blob_threshold(monkeypatch, 10_000)
        image = ImageArtifact(value=b"\x00" * 30, format="png", width=1, height=1)
        result = SetParameterValueResultSuccess(finalized_value=image, data_type="ImageArtifact", result_details="ok")
        event = EventResultSuccess(
            request=SetParameterValueRequest(parameter_name="p", node_name="n", value=None), result=result
        )

        assert event.dict()["result"]["finalized_value"]["value"] is not None

    def test_execution_event_dict_blanks_tagged_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_blob_threshold(monkeypatch, 100)
        image = ImageArtifact(value=b"\x00" * 300, format="png", width=1, height=1)
        payload = NodeResolvedEvent(node_name="n", parameter_output_values={"out": image}, node_type="T")
        event = ExecutionEvent(payload=payload)

        wire = event.dict()

        assert wire["payload"]["parameter_output_values"]["out"]["value"] is None

    def test_event_result_dict_blanks_the_echoed_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The result echoes the request back to clients; a request carrying a blob (e.g. workflow-load
        # SetParameterValue) must be blanked in that echo too.
        _mock_blob_threshold(monkeypatch, 100)
        request = SetParameterValueRequest(parameter_name="p", node_name="n", value=_blob_artifact(300))
        result = SetParameterValueResultSuccess(finalized_value=None, data_type="ImageArtifact", result_details="ok")

        wire = EventResultSuccess(request=request, result=result).dict()

        assert wire["request"]["value"]["value"] is None


class TestEveryTaggedFieldBlanksWhenSerialized:
    """Serialize each tagged carrier with a real blob artifact, then run the real tag-driven strip.

    This is the authoritative end-to-end check: it uses the real ``safe_unstructure`` serialization of
    each payload and drives ``_strip_tagged_blob_fields`` (which reads the field tag, exactly as
    production's EventResult.dict()/ExecutionEvent.dict() do). So it catches both a tagged field whose
    real shape (direct value / dict-of-values / list / nested) the walker mishandles, AND a field that
    lost (or moved) its blob tag.
    """

    @pytest.mark.parametrize("payload_type", list(_BLOB_CASE_BUILDERS), ids=_BLOB_TAGGED_FIELD_IDS)
    def test_tagged_field_blanks(self, payload_type: type, monkeypatch: pytest.MonkeyPatch) -> None:
        factory, path = _BLOB_CASE_BUILDERS[payload_type]
        payload = factory(_tiny_image_artifact())
        serialized = safe_unstructure(payload)

        # Positive control: the blob really is at the expected path as a base64 string before stripping.
        assert isinstance(_navigate(serialized, path), str), (
            f"{payload_type.__name__}: no base64 blob at {path} (serialized shape changed?)"
        )

        _mock_blob_threshold(monkeypatch, 4)
        _blank_oversized_tagged_blob_fields(payload, serialized)  # tag-driven, exactly as production serialization does

        assert _navigate(serialized, path) is None, (
            f"{payload_type.__name__}: blob at {path} not blanked (field lost its tag, or shape unhandled?)"
        )
        assert _surviving_blob_str_values(serialized[path[0]]) == [], (
            f"{payload_type.__name__}.{path[0]}: a blob value survived blanking"
        )

    def test_builders_cover_exactly_the_tagged_payloads(self) -> None:
        # Ground truth = payloads that actually carry a blob tag. _BLOB_CASE_BUILDERS must match it, so a
        # newly-tagged (or newly-untagged) carrier fails here until the builders dict is updated.
        tagged = _blob_tagged_payload_types()
        missing = {cls.__name__ for cls in tagged - set(_BLOB_CASE_BUILDERS)}
        extra = {cls.__name__ for cls in set(_BLOB_CASE_BUILDERS) - tagged}
        assert not missing, f"Tagged payloads with no blob-case builder: {sorted(missing)}"
        assert not extra, f"Blob-case builders for payloads that are not tagged: {sorted(extra)}"

    @pytest.mark.parametrize(
        "artifact",
        [
            ImageArtifact(value=b"blob" * 8, format="png", width=1, height=1),
            AudioArtifact(value=b"blob" * 8, format="wav"),
            BlobArtifact(value=b"blob" * 8),
        ],
        ids=["ImageArtifact", "AudioArtifact", "BlobArtifact"],
    )
    def test_real_artifact_type_blanks(self, artifact: Any) -> None:
        # All three blob-backed artifact types serialize to the {type, value: <b64>} shape the walker blanks.
        serialized = safe_unstructure(artifact)

        blanked = _blank_oversized_blobs(serialized, max_b64_bytes=4)

        assert blanked
        assert serialized["value"] is None

    def test_list_valued_field_blanks(self) -> None:
        # A tagged Any field can hold a list of artifacts (e.g. a node emitting list[ImageArtifact]).
        payload = ParameterValueUpdateEvent(
            node_name="n", parameter_name="p", data_type="list", value=[_tiny_image_artifact(), _tiny_image_artifact()]
        )
        serialized = safe_unstructure(payload)

        blanked = _blank_oversized_blobs(serialized["value"], max_b64_bytes=4)

        assert len(blanked) == 2  # noqa: PLR2004
        assert _surviving_blob_str_values(serialized["value"]) == []


def _blob_artifact(size: int) -> dict:
    """A serialized blob-artifact dict with a base64 ``value`` of ``size`` characters."""
    return {"type": "ImageArtifact", "value": "A" * size, "format": "png"}


def _mock_blob_threshold(monkeypatch: pytest.MonkeyPatch, value: int) -> Mock:
    """Force the blob-size threshold via a spec'd ConfigManager mock (base_events reads it lazily)."""
    config_manager = Mock(spec=ConfigManager)
    config_manager.get_config_value.return_value = value
    monkeypatch.setattr(
        "griptape_nodes.retained_mode.griptape_nodes.GriptapeNodes.ConfigManager",
        lambda: config_manager,
    )
    return config_manager


def _navigate(serialized: Any, path: tuple[str, ...]) -> Any:
    """Follow a key-path into a serialized structure (raises KeyError if the expected shape is gone)."""
    for key in path:
        serialized = serialized[key]
    return serialized


# Event-package modules that are NOT payload definitions and must not be imported for discovery.
# generate_request_payload_schemas is a script: at import it builds pydantic models for every registered
# payload and writes request_payload_schemas.json (and trips on an unresolved forward ref).
_NON_PAYLOAD_EVENT_MODULES = frozenset({"generate_request_payload_schemas"})


def _all_registered_payload_types() -> set[type]:
    """Every registered Payload subclass -- importing all event modules first so the registry is complete."""
    import griptape_nodes.retained_mode.events as events_pkg

    for module_info in pkgutil.iter_modules(events_pkg.__path__, f"{events_pkg.__name__}."):
        if module_info.name.rsplit(".", 1)[-1] in _NON_PAYLOAD_EVENT_MODULES:
            continue
        importlib.import_module(module_info.name)
    return set(PayloadRegistry.get_registry().values())


def _blob_tagged_payload_types() -> set[type]:
    """Payload types that actually carry a blob-tagged field (the ground truth _BLOB_CASE_BUILDERS must match)."""
    return {
        cls
        for cls in _all_registered_payload_types()
        if is_dataclass(cls) and any(f.metadata.get(BLOB_FIELD_METADATA_KEY) for f in fields(cls))
    }


def _surviving_blob_str_values(serialized: Any) -> list[str]:
    """All base64 string values still present under a blob-artifact dict (should be empty after blanking)."""
    found: list[str] = []
    if isinstance(serialized, dict):
        if serialized.get("type") in _BLOB_ARTIFACT_TYPE_NAMES and isinstance(serialized.get("value"), str):
            found.append(serialized["value"])
        for child in serialized.values():
            found.extend(_surviving_blob_str_values(child))
    elif isinstance(serialized, list):
        for child in serialized:
            found.extend(_surviving_blob_str_values(child))
    return found


def _tiny_image_artifact() -> ImageArtifact:
    """A real ImageArtifact whose base64 value (~44 chars) comfortably exceeds a 4-byte threshold."""
    return ImageArtifact(value=b"blob" * 8, format="png", width=1, height=1)
