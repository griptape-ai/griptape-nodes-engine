"""Tests for event_converter structure/unstructure hooks."""

from typing import Any

import pytest

from griptape_nodes.retained_mode.events.base_events import EventRequest, ForwardedException
from griptape_nodes.retained_mode.events.event_converter import (
    _is_json_primitive_union,
    converter,
)
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest


class TestIsJsonPrimitiveUnion:
    """Test the _is_json_primitive_union predicate."""

    def test_matches_union_of_json_primitives(self) -> None:
        assert _is_json_primitive_union(str | int | float | bool | dict | list | None) is True

    def test_matches_partial_union(self) -> None:
        assert _is_json_primitive_union(dict | list | None) is True

    def test_matches_two_member_union(self) -> None:
        assert _is_json_primitive_union(dict | list) is True

    def test_rejects_non_union(self) -> None:
        assert _is_json_primitive_union(str) is False

    def test_rejects_union_with_non_primitive(self) -> None:
        assert _is_json_primitive_union(str | bytes) is False


class TestJsonPrimitiveUnionStructuring:
    """Test that the converter structures JSON-primitive Union types correctly."""

    @pytest.fixture
    def union_type(self) -> Any:
        return str | int | float | bool | dict | list | None

    def test_structure_str(self, union_type: type) -> None:
        assert converter.structure("hello", union_type) == "hello"

    def test_structure_int(self, union_type: type) -> None:
        value = 42
        assert converter.structure(value, union_type) == value

    def test_structure_float(self, union_type: type) -> None:
        value = 3.14
        assert converter.structure(value, union_type) == value

    def test_structure_bool(self, union_type: type) -> None:
        assert converter.structure(True, union_type) is True

    def test_structure_none(self, union_type: type) -> None:
        assert converter.structure(None, union_type) is None

    def test_structure_list(self, union_type: type) -> None:
        assert converter.structure([1, 2, 3], union_type) == [1, 2, 3]

    def test_structure_dict(self, union_type: type) -> None:
        assert converter.structure({"key": "value"}, union_type) == {"key": "value"}

    def test_structure_nested_dict(self, union_type: type) -> None:
        value = {"outer": {"inner": [1, 2, 3]}}
        assert converter.structure(value, union_type) == value


class TestSetParameterValueRequestStructuring:
    """Test that SetParameterValueRequest structures correctly with complex values."""

    def test_structure_with_dict_value(self) -> None:
        data = {
            "node_name": "Load Image",
            "parameter_name": "image",
            "value": {"url": "http://example.com/image.jpg", "width": 100},
        }
        result = converter.structure(data, SetParameterValueRequest)

        assert result.node_name == "Load Image"
        assert result.parameter_name == "image"
        assert result.value == {"url": "http://example.com/image.jpg", "width": 100}

    def test_structure_with_list_value(self) -> None:
        data = {
            "node_name": "MyNode",
            "parameter_name": "items",
            "value": [1, 2, 3],
        }
        result = converter.structure(data, SetParameterValueRequest)

        assert result.value == [1, 2, 3]

    def test_structure_with_string_value(self) -> None:
        data = {
            "node_name": "MyNode",
            "parameter_name": "name",
            "value": "hello",
        }
        result = converter.structure(data, SetParameterValueRequest)

        assert result.value == "hello"

    def test_structure_with_none_value(self) -> None:
        data = {
            "node_name": "MyNode",
            "parameter_name": "name",
            "value": None,
        }
        result = converter.structure(data, SetParameterValueRequest)

        assert result.value is None

    def test_from_dict_with_image_artifact_value(self) -> None:
        """Reproduce the exact payload that triggered the original bug."""
        data = {
            "event_type": "EventRequest",
            "request_type": "SetParameterValueRequest",
            "request_id": "bd1743f3-7508-429f-bad1-55cd47e9e181",
            "response_topic": "sessions/abc123/response",
            "request": {
                "node_name": "Load Image",
                "parameter_name": "image",
                "value": {
                    "value": "http://localhost:8124/workspace/inputs/IMG_0798.jpeg",
                    "width": 3024,
                    "height": 4032,
                    "name": "IMG_0798.jpeg",
                    "type": "ImageUrlArtifact",
                    "meta": {
                        "created_at": "2026-04-14T20:12:28.745Z",
                        "content_hash": "",
                        "size_bytes": 7996029,
                        "format": "JPEG",
                    },
                },
            },
        }
        event = EventRequest.from_dict(data)

        assert isinstance(event.request, SetParameterValueRequest)
        assert event.request.node_name == "Load Image"
        assert event.request.parameter_name == "image"
        assert isinstance(event.request.value, dict)
        assert event.request.value["type"] == "ImageUrlArtifact"
        expected_width = 3024
        assert event.request.value["width"] == expected_width


class TestExceptionWireForm:
    """Round-trip coverage for the Exception <-> dict converter pair.

    The unstructure hook emits ``{type, message, traceback}`` and the
    structure hook rebuilds those into ``ForwardedException``'s
    ``original_type`` / message / ``original_traceback`` slots. Both
    halves are load-bearing for the orchestrator-side
    ``[<type>] ... Worker traceback: ...`` rendering in
    ``NodeExecutor._format_node_failure_message``.
    """

    @staticmethod
    def _raise_and_capture(exc: Exception) -> Exception:
        try:
            raise exc  # noqa: TRY301
        except Exception as e:
            return e

    def test_unstructure_raised_exception_carries_type_message_and_traceback(self) -> None:
        e = self._raise_and_capture(ValueError("boom"))
        payload = converter.unstructure(e, Exception)

        assert payload["type"] == "builtins.ValueError"
        assert payload["message"] == "boom"
        assert payload["traceback"] is not None
        assert "ValueError: boom" in payload["traceback"]

    def test_unstructure_unraised_exception_has_null_traceback(self) -> None:
        # An exception that was constructed but never raised has
        # ``__traceback__ is None``; the wire form preserves type and
        # message but the traceback slot is null.
        payload = converter.unstructure(ValueError("never raised"), Exception)

        assert payload["type"] == "builtins.ValueError"
        assert payload["message"] == "never raised"
        assert payload["traceback"] is None

    def test_round_trip_yields_forwarded_exception_with_worker_fields(self) -> None:
        e = self._raise_and_capture(RuntimeError("worker boom"))
        payload = converter.unstructure(e, Exception)
        rebuilt = converter.structure(payload, Exception)

        assert isinstance(rebuilt, ForwardedException)
        assert str(rebuilt) == "worker boom"
        assert rebuilt.original_type == "builtins.RuntimeError"
        assert rebuilt.original_traceback is not None
        assert "RuntimeError: worker boom" in rebuilt.original_traceback

    def test_structure_tolerates_non_dict_payload(self) -> None:
        # Old persisted events on disk may carry a bare-string
        # ``exception`` field; refusing to structure them would abort
        # deserialization of the whole enclosing event.
        rebuilt = converter.structure("legacy stringified error", Exception)

        assert isinstance(rebuilt, ForwardedException)
        assert str(rebuilt) == "legacy stringified error"
        assert rebuilt.original_type is None
        assert rebuilt.original_traceback is None
