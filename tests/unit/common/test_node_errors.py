"""Contract tests for ``build_node_error_details`` and the ``NodeError`` wire form.

``NodeErrorEvent.error`` carries a node failure in parts so the editor does not have to parse the
flattened ``error_message``. These tests pin the acceptance criteria from #5733: the node's own
words, no engine preamble or node name prefix, the exception type, one entry per validation
exception, and ``NodeError`` attachments that survive the worker boundary.
"""

import json
from typing import Any

from griptape_nodes.common.node_errors import NodeExecutionError, build_node_error_details
from griptape_nodes.common.node_executor import NodeExecutor
from griptape_nodes.exe_types.core_types import NodeError, NodeErrorLink
from griptape_nodes.retained_mode.events.base_events import ForwardedException, ForwardedNodeError
from griptape_nodes.retained_mode.events.event_converter import converter
from griptape_nodes.retained_mode.events.execution_events import ExecuteNodeResultFailure, NodeErrorEvent
from griptape_nodes.retained_mode.events.node_error_details import (
    MAX_RESPONSE_BYTES,
    RESPONSE_DROPPED_FIELD,
)

NODE_NAME = "Get Dictionary Value by Key"


class _MissingSettingError(KeyError):
    def __str__(self) -> str:
        return f"Setting {self.args[0]!r} is missing"


class _PlainKeySubclassError(KeyError):
    pass


def _raised(exc: Exception) -> Exception:
    try:
        raise exc  # noqa: TRY301
    except Exception as e:
        return e


def _across_worker(exc: Exception) -> Exception:
    """Send an exception through the converter the way a worker's result does."""
    wire = json.loads(json.dumps(converter.unstructure(_raised(exc), Exception)))
    return converter.structure(wire, Exception)


def _executor_error(result: ExecuteNodeResultFailure) -> NodeExecutionError:
    """Build the error exactly as ``NodeExecutor.execute`` raises it for a failed result."""
    message = NodeExecutor._format_node_failure_message(NODE_NAME, result, result.exception)
    return NodeExecutionError(
        message,
        result_details=str(result.result_details),
        exception=result.exception,
        validation_exceptions=result.validation_exceptions,
    )


def _failed_while_running(exc: Exception) -> ExecuteNodeResultFailure:
    return ExecuteNodeResultFailure(
        result_details=f"Attempted to execute node '{NODE_NAME}'. Failed with error: {exc}",
        exception=exc,
    )


class TestMessage:
    def test_key_error_in_process_has_no_quotes_type_or_name_prefix(self) -> None:
        exc = _raised(KeyError(f"{NODE_NAME}: Key 'b' not found"))

        details = build_node_error_details(NODE_NAME, _executor_error(_failed_while_running(exc)))

        assert details.message == "Key 'b' not found"
        assert details.exception_type == "builtins.KeyError"

    def test_key_error_from_worker_has_no_quotes_type_or_name_prefix(self) -> None:
        forwarded = _across_worker(KeyError(f"{NODE_NAME}: Key 'b' not found"))

        details = build_node_error_details(NODE_NAME, _executor_error(_failed_while_running(forwarded)))

        assert details.message == "Key 'b' not found"
        assert details.exception_type == "builtins.KeyError"

    def test_non_string_key_error_from_worker_keeps_its_text(self) -> None:
        details = build_node_error_details(NODE_NAME, _across_worker(KeyError(5)))

        assert details.message == "5"

    def test_key_error_subclass_keeps_its_own_str_on_both_paths(self) -> None:
        exc = _MissingSettingError("strength")

        in_process = build_node_error_details(NODE_NAME, _raised(exc))
        from_worker = build_node_error_details(NODE_NAME, _across_worker(exc))

        assert in_process.message == "Setting 'strength' is missing"
        assert from_worker.message == "Setting 'strength' is missing"

    def test_key_error_subclass_without_its_own_str_matches_on_both_paths(self) -> None:
        exc = _PlainKeySubclassError("strength")

        in_process = build_node_error_details(NODE_NAME, _raised(exc))
        from_worker = build_node_error_details(NODE_NAME, _across_worker(exc))

        assert in_process.message == from_worker.message == "'strength'"

    def test_engine_preambles_never_reach_the_message(self) -> None:
        error = _executor_error(_failed_while_running(_raised(ValueError("Image is required"))))

        details = build_node_error_details(NODE_NAME, error)

        assert "Attempted to execute node" in str(error)
        assert "execution failed" in str(error)
        assert details.message == "Image is required"

    def test_only_an_exact_leading_name_prefix_is_removed(self) -> None:
        details = build_node_error_details(NODE_NAME, ValueError(f"Input to {NODE_NAME}: was empty"))

        assert details.message == f"Input to {NODE_NAME}: was empty"

    def test_engine_failure_without_an_exception_uses_result_details(self) -> None:
        result = ExecuteNodeResultFailure(result_details=f"{NODE_NAME}: no worker is available")

        details = build_node_error_details(NODE_NAME, _executor_error(result))

        assert details.message == "no worker is available"
        assert details.exception_type is None

    def test_other_exceptions_use_their_own_message(self) -> None:
        details = build_node_error_details(NODE_NAME, _raised(ZeroDivisionError("division by zero")))

        assert details.message == "division by zero"
        assert details.exception_type == "builtins.ZeroDivisionError"
        assert details.messages is None


class TestValidation:
    def test_pre_run_validation_list_gives_one_message_per_exception(self) -> None:
        exceptions: list[Exception] = [
            ValueError(f"{NODE_NAME}: Image is required for editing."),
            KeyError("Prompt is missing"),
        ]

        details = build_node_error_details(NODE_NAME, exceptions)

        assert details.messages == ["Image is required for editing.", "Prompt is missing"]
        assert details.message == "Image is required for editing."
        assert details.exception_type == "builtins.ValueError"

    def test_declined_to_run_carries_its_exceptions(self) -> None:
        result = ExecuteNodeResultFailure(
            result_details="declined",
            validation_exceptions=[ValueError("Needs a GPU"), ValueError("Needs torch")],
        )

        details = build_node_error_details(NODE_NAME, _executor_error(result))

        assert details.messages == ["Needs a GPU", "Needs torch"]

    def test_single_validation_exception_still_sets_messages(self) -> None:
        details = build_node_error_details(NODE_NAME, [ValueError("Only one")])

        assert details.messages == ["Only one"]


class TestErrorMessageUnchanged:
    def test_executor_message_matches_the_formatted_failure(self) -> None:
        result = _failed_while_running(_raised(KeyError("Key 'b' not found")))

        error = _executor_error(result)

        assert str(error) == NodeExecutor._format_node_failure_message(NODE_NAME, result, result.exception)

    def test_worker_key_error_keeps_its_quoted_str(self) -> None:
        original = KeyError("Key 'b' not found")

        forwarded = _across_worker(original)

        assert str(forwarded) == str(original)


class TestNodeErrorAttachments:
    def _node_error(self, **kwargs: Any) -> NodeError:
        return NodeError("Processing failed: proxy client error", **kwargs)

    def test_fields_and_response_survive_in_process(self) -> None:
        exc = self._node_error(fields={"generation_id": "90db"}, response={"status": "ERRORED"})

        details = build_node_error_details(NODE_NAME, exc)

        assert details.fields == {"generation_id": "90db"}
        assert details.response == {"status": "ERRORED"}

    def test_fields_and_response_survive_the_worker_boundary(self) -> None:
        forwarded = _across_worker(self._node_error(fields={"generation_id": "90db"}, response={"status": "ERRORED"}))

        details = build_node_error_details(NODE_NAME, forwarded)

        assert isinstance(forwarded, ForwardedException)
        assert details.message == "Processing failed: proxy client error"
        assert details.exception_type == "griptape_nodes.exe_types.node_error.NodeError"
        assert details.fields == {"generation_id": "90db"}
        assert details.response == {"status": "ERRORED"}

    def test_numeric_field_values_are_shown_as_text(self) -> None:
        details = build_node_error_details(NODE_NAME, self._node_error(fields={"status_code": 400}))

        assert details.fields == {"status_code": "400"}

    def test_oversized_response_is_dropped_with_a_marker(self) -> None:
        big = {"image": "A" * MAX_RESPONSE_BYTES}

        in_process = build_node_error_details(NODE_NAME, self._node_error(response=big))
        from_worker = build_node_error_details(NODE_NAME, _across_worker(self._node_error(response=big)))

        for details in (in_process, from_worker):
            assert details.response is None
            assert details.fields == {RESPONSE_DROPPED_FIELD: "true"}

    def test_unserializable_response_is_dropped_without_failing(self) -> None:
        details = build_node_error_details(NODE_NAME, self._node_error(response={"when": object()}))

        assert details.response is None

    def test_response_with_nan_is_dropped_with_a_marker(self) -> None:
        exc = self._node_error(response={"score": float("nan")})

        in_process = build_node_error_details(NODE_NAME, exc)
        from_worker = build_node_error_details(NODE_NAME, _across_worker(exc))

        for details in (in_process, from_worker):
            assert details.response is None
            assert details.fields == {RESPONSE_DROPPED_FIELD: "true"}

    def test_deeply_nested_response_is_dropped_without_failing(self) -> None:
        # Deep enough to exceed the C JSON encoder's own recursion limit, which is higher than
        # sys.getrecursionlimit().
        depth = 10_000
        nested: dict[str, Any] = {}
        innermost = nested
        for _ in range(depth):
            innermost["next"] = {}
            innermost = innermost["next"]

        details = build_node_error_details(NODE_NAME, self._node_error(response=nested))

        assert details.response is None
        assert details.message == "Processing failed: proxy client error"

    def test_https_link_survives_and_javascript_link_is_dropped(self) -> None:
        links = [
            NodeErrorLink(label="Supported image formats", url="https://docs.griptapenodes.com/formats"),
            NodeErrorLink(label="Click", url="javascript:alert(1)"),
        ]
        expected = [NodeErrorLink(label="Supported image formats", url="https://docs.griptapenodes.com/formats")]

        in_process = build_node_error_details(NODE_NAME, self._node_error(links=links))
        from_worker = build_node_error_details(NODE_NAME, _across_worker(self._node_error(links=links)))

        assert in_process.links == expected
        assert from_worker.links == expected
        assert from_worker.message == "Processing failed: proxy client error"

    def test_links_are_capped_at_three_with_short_labels(self) -> None:
        links = [NodeErrorLink(label="x" * 200, url=f"https://example.com/{i}") for i in range(5)]

        details = build_node_error_details(NODE_NAME, self._node_error(links=links))

        expected_link_count = 3
        expected_label_length = 80
        assert len(details.links) == expected_link_count
        assert all(len(link.label) == expected_label_length for link in details.links)

    def test_attachments_survive_being_forwarded_twice(self) -> None:
        exc = self._node_error(fields={"generation_id": "90db"}, response={"status": "ERRORED"})
        first_hop = _across_worker(exc)

        second_hop = converter.structure(json.loads(json.dumps(converter.unstructure(first_hop, Exception))), Exception)
        details = build_node_error_details(NODE_NAME, second_hop)

        assert isinstance(second_hop, ForwardedNodeError)
        assert details.fields == {"generation_id": "90db"}
        assert details.response == {"status": "ERRORED"}

    def test_only_a_node_error_crosses_as_a_forwarded_node_error(self) -> None:
        plain = _across_worker(ValueError("boom"))
        node_error = _across_worker(self._node_error())

        assert type(plain) is ForwardedException
        assert isinstance(node_error, ForwardedNodeError)

    def test_attributes_on_other_exceptions_are_ignored(self) -> None:
        exc = ValueError("boom")
        exc.fields = {"looks": "like a NodeError"}  # type: ignore[attr-defined]
        exc.response = {"status": 500}  # type: ignore[attr-defined]

        details = build_node_error_details(NODE_NAME, exc)

        assert details.fields == {}
        assert details.response is None


class TestEventWireForm:
    def test_node_error_event_round_trips_with_details(self) -> None:
        exc = NodeError(
            "Bad input", fields={"request_id": "r1"}, links=[NodeErrorLink(label="Docs", url="https://x.y")]
        )
        event = NodeErrorEvent(
            node_name=NODE_NAME, error_message=str(exc), error=build_node_error_details(NODE_NAME, exc)
        )

        wire = json.loads(json.dumps(converter.unstructure(event)))
        rebuilt = converter.structure(wire, NodeErrorEvent)

        assert rebuilt == event

    def test_node_error_event_without_details_still_parses(self) -> None:
        rebuilt = converter.structure({"node_name": NODE_NAME, "error_message": "old engine"}, NodeErrorEvent)

        assert rebuilt.error is None
