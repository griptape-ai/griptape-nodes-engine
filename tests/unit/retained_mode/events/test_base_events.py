"""Tests for RequestPayload base class broadcast_result behavior."""

import logging

import pytest

from griptape_nodes.retained_mode.events.base_events import (
    RequestPayload,
    ResultDetail,
    ResultDetails,
    StrictModeViolationDetail,
)
from griptape_nodes.retained_mode.events.event_converter import converter
from griptape_nodes.retained_mode.events.os_events import ReadFileRequest
from griptape_nodes.retained_mode.events.path_filter import apply_path_tree, build_path_tree


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
