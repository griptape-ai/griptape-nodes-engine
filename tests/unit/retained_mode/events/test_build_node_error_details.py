"""Contract tests for ``NodeErrorEvent.error``: what the editor receives for a failed node.

The details are built where the node fails, ride back on ``ExecuteNodeResultFailure.error``, and
are read where the event is sent. These tests pin the acceptance criteria from #5733: the node's own
words, no engine preamble or node name prefix, the exception type, one entry per validation
exception, and ``NodeError`` attachments that survive the worker boundary.
"""

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from griptape_nodes.common.node_executor import ExecuteNodeFailedError, NodeExecutor
from griptape_nodes.exe_types.core_types import NodeError, NodeErrorLink
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.base_events import ForwardedException
from griptape_nodes.retained_mode.events.execution_events import ExecuteNodeResultFailure, NodeErrorEvent
from griptape_nodes.retained_mode.events.node_error_details import (
    MAX_PRINTED_RESPONSE_MESSAGE_CHARS,
    MAX_RESPONSE_BYTES,
    RESPONSE_DROPPED_FIELD,
    NodeErrorDetails,
    build_node_error_details,
)
from griptape_nodes.retained_mode.events.worker_events import WorkerGoneError
from griptape_nodes.serialization.converter import converter

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


def _across_worker(result: ExecuteNodeResultFailure) -> ExecuteNodeResultFailure:
    """Send a result through the converter the way a worker sends it back."""
    wire = json.loads(json.dumps(converter.unstructure(result)))
    return converter.structure(wire, ExecuteNodeResultFailure)


def _executor_error(result: ExecuteNodeResultFailure) -> ExecuteNodeFailedError:
    """The error ``NodeExecutor.execute`` raises for a failed result."""
    executor = NodeExecutor(engine=MagicMock())
    return executor._execute_node_failed_error(NODE_NAME, result, result.exception)


def _emitted(result: ExecuteNodeResultFailure) -> NodeErrorDetails:
    return _executor_error(result).details


def _both_paths(result: ExecuteNodeResultFailure) -> list[NodeErrorDetails]:
    return [_emitted(result), _emitted(_across_worker(result))]


def _declined(exceptions: list[Exception]) -> ExecuteNodeResultFailure:
    """Shaped like node_manager's result for a node that declined to run."""
    return ExecuteNodeResultFailure(
        result_details="declined",
        validation_exceptions=exceptions,
        error=build_node_error_details(NODE_NAME, exceptions),
    )


@pytest.fixture
def failed_while_running(engine: Engine) -> Any:
    """Build the result node_manager returns when a node's ``aprocess`` raises."""

    def build(exc: Exception) -> ExecuteNodeResultFailure:
        return engine.node_manager._execution_failure(_raised(exc), NODE_NAME)

    return build


class TestMessage:
    def test_key_error_has_no_quotes_type_or_name_prefix_on_both_paths(self, failed_while_running: Any) -> None:
        result = failed_while_running(KeyError(f"{NODE_NAME}: Key 'b' not found"))

        for details in _both_paths(result):
            assert details.message == "Key 'b' not found"
            assert details.exception_type == "builtins.KeyError"

    def test_key_error_without_arguments_keeps_its_empty_text(self, failed_while_running: Any) -> None:
        for details in _both_paths(failed_while_running(KeyError())):
            assert details.message == ""
            assert details.exception_type == "builtins.KeyError"

    def test_non_string_key_error_keeps_its_text(self, failed_while_running: Any) -> None:
        for details in _both_paths(failed_while_running(KeyError(5))):
            assert details.message == "5"

    def test_key_error_subclass_keeps_its_own_str_on_both_paths(self, failed_while_running: Any) -> None:
        for details in _both_paths(failed_while_running(_MissingSettingError("strength"))):
            assert details.message == "Setting 'strength' is missing"

    def test_key_error_subclass_without_its_own_str_drops_the_quoting_on_both_paths(
        self, failed_while_running: Any
    ) -> None:
        for details in _both_paths(failed_while_running(_PlainKeySubclassError("strength"))):
            assert details.message == "strength"

    def test_engine_preambles_never_reach_the_message(self, failed_while_running: Any) -> None:
        error = _executor_error(failed_while_running(ValueError("Image is required")))

        assert "Attempted to execute node" in str(error)
        assert "execution failed" in str(error)
        assert error.details.message == "Image is required"

    def test_only_an_exact_leading_name_prefix_is_removed(self) -> None:
        details = build_node_error_details(NODE_NAME, ValueError(f"Input to {NODE_NAME}: was empty"))

        assert details.message == f"Input to {NODE_NAME}: was empty"

    def test_other_exceptions_use_their_own_message(self) -> None:
        details = build_node_error_details(NODE_NAME, _raised(ZeroDivisionError("division by zero")))

        assert details.message == "division by zero"
        assert details.exception_type == "builtins.ZeroDivisionError"
        assert details.messages is None


class TestEngineWrittenFailures:
    def test_engine_failure_without_an_exception_uses_result_details(self) -> None:
        result = ExecuteNodeResultFailure(result_details=f"{NODE_NAME}: no worker is available")

        details = _emitted(result)

        assert details.message == "no worker is available"
        assert details.exception_type is None

    def test_engine_written_failure_keeps_the_engines_message(self) -> None:
        # Shaped like node_manager's WorkerGoneError failure: the engine wrote result_details for
        # the user, and the exception is only the cause.
        cause = _raised(WorkerGoneError("worker 'a1b2c3' stopped responding and was shut down."))
        details_text = (
            f"Attempted to run node '{NODE_NAME}' in a separate process. Failed because {cause} "
            "Editing the node still works and your workflow keeps it."
        )
        result = ExecuteNodeResultFailure(result_details=details_text, exception=cause)

        details = _emitted(result)

        assert details.message == details_text
        assert details.exception_type == "griptape_nodes.retained_mode.events.worker_events.WorkerGoneError"

    def test_parameter_set_failure_keeps_which_parameter(self) -> None:
        # Shaped like node_manager's set_parameter_value failure, which names the parameter.
        cause = _raised(ValueError("must be a positive number"))
        details_text = f"Attempted to set parameter 'steps' on node '{NODE_NAME}'. Failed with error: {cause}"
        result = ExecuteNodeResultFailure(result_details=details_text, exception=cause)

        details = _emitted(result)

        assert details.message == details_text
        assert details.exception_type == "builtins.ValueError"

    def test_engine_written_failure_from_a_worker_keeps_the_engines_message(self) -> None:
        result = _across_worker(
            ExecuteNodeResultFailure(
                result_details="Attempted to run the node. Failed because the worker stopped responding.",
                exception=_raised(RuntimeError("worker 'a1b2c3' gone")),
            )
        )

        details = _emitted(result)

        assert isinstance(result.exception, ForwardedException)
        assert details.message == "Attempted to run the node. Failed because the worker stopped responding."
        assert details.exception_type == "builtins.RuntimeError"


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

    def test_declined_to_run_carries_its_exceptions_on_both_paths(self) -> None:
        result = _declined([ValueError("Needs a GPU"), ValueError("Needs torch")])

        for details in _both_paths(result):
            assert details.messages == ["Needs a GPU", "Needs torch"]

    def test_empty_validation_list_still_reads_as_a_validation_failure(self) -> None:
        details = build_node_error_details(NODE_NAME, [])

        assert details.messages == []
        assert details.message == "The node failed validation but did not say why."

    def test_single_validation_exception_still_sets_messages(self) -> None:
        details = build_node_error_details(NODE_NAME, [ValueError("Only one")])

        assert details.messages == ["Only one"]

    def test_attachments_of_a_later_node_error_are_kept_on_both_paths(self) -> None:
        link = NodeErrorLink(label="Add the API key", url="#settings-secrets?filter=MY_KEY")
        missing_key = NodeError("API key MY_KEY is missing.", fields={"secret": "MY_KEY"}, links=[link])

        for details in _both_paths(_declined([ValueError("Prompt is required."), missing_key])):
            assert details.message == "Prompt is required."
            assert details.messages == ["Prompt is required.", "API key MY_KEY is missing."]
            assert details.fields == {"secret": "MY_KEY"}
            assert details.links == [link]

    def test_attachments_from_several_node_errors_are_merged(self) -> None:
        def docs(i: int) -> NodeErrorLink:
            return NodeErrorLink(label=f"Docs {i}", url=f"https://docs.griptapenodes.com/{i}")

        first = NodeError("First", fields={"request_id": "r1"}, response={"status": "A"}, links=[docs(1), docs(2)])
        second = NodeError(
            "Second",
            fields={"request_id": "r2", "error_code": "E7"},
            response={"status": "B"},
            links=[docs(2), docs(3), docs(4)],
        )

        exceptions: list[Exception] = [first, second]
        details = build_node_error_details(NODE_NAME, exceptions)

        assert details.fields == {"request_id": "r1", "error_code": "E7"}
        assert details.response == {"status": "A"}
        assert details.links == [docs(1), docs(2), docs(3)]

    def test_dropped_marker_is_removed_when_another_response_is_kept(self) -> None:
        kept = NodeError("Kept", response={"status": "A"})
        dropped = NodeError("Dropped", response={"image": "A" * MAX_RESPONSE_BYTES})

        orders: list[list[Exception]] = [[kept, dropped], [dropped, kept]]
        for exceptions in orders:
            details = build_node_error_details(NODE_NAME, exceptions)

            assert details.response == {"status": "A"}
            assert RESPONSE_DROPPED_FIELD not in details.fields

    def test_dropped_marker_stays_when_no_response_is_kept(self) -> None:
        dropped = NodeError("Dropped", response={"image": "A" * MAX_RESPONSE_BYTES})

        details = build_node_error_details(NODE_NAME, [ValueError("Prompt is required."), dropped])

        assert details.response is None
        assert details.fields == {RESPONSE_DROPPED_FIELD: "true"}


class TestErrorMessageUnchanged:
    def test_executor_message_matches_the_formatted_failure(self, failed_while_running: Any) -> None:
        result = failed_while_running(KeyError("Key 'b' not found"))

        error = _executor_error(result)

        assert str(error) == NodeExecutor._format_node_failure_message(NODE_NAME, result, result.exception)

    def test_worker_key_error_keeps_its_quoted_str(self, failed_while_running: Any) -> None:
        original = KeyError("Key 'b' not found")

        forwarded = _across_worker(failed_while_running(original)).exception

        assert str(forwarded) == str(original)


class TestNodeErrorAttachments:
    def _node_error(self, **kwargs: Any) -> NodeError:
        return NodeError("Processing failed: proxy client error", **kwargs)

    def test_fields_and_response_survive_on_both_paths(self, failed_while_running: Any) -> None:
        result = failed_while_running(
            self._node_error(fields={"generation_id": "90db"}, response={"status": "ERRORED"})
        )

        for details in _both_paths(result):
            assert details.message == "Processing failed: proxy client error"
            assert details.exception_type == "griptape_nodes.exe_types.node_error.NodeError"
            assert details.fields == {"generation_id": "90db"}
            assert details.response == {"status": "ERRORED"}

    def test_numeric_field_values_are_shown_as_text(self) -> None:
        details = build_node_error_details(NODE_NAME, self._node_error(fields={"status_code": 400}))

        assert details.fields == {"status_code": "400"}

    def test_oversized_response_is_dropped_with_a_marker(self, failed_while_running: Any) -> None:
        result = failed_while_running(self._node_error(response={"image": "A" * MAX_RESPONSE_BYTES}))

        for details in _both_paths(result):
            assert details.response is None
            assert details.fields == {RESPONSE_DROPPED_FIELD: "true"}

    def test_unserializable_response_is_dropped_without_failing(self) -> None:
        details = build_node_error_details(NODE_NAME, self._node_error(response={"when": object()}))

        assert details.response is None

    def test_response_with_nan_is_dropped_with_a_marker(self, failed_while_running: Any) -> None:
        result = failed_while_running(self._node_error(response={"score": float("nan")}))

        for details in _both_paths(result):
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

    def test_https_link_survives_and_javascript_link_is_dropped(self, failed_while_running: Any) -> None:
        links = [
            NodeErrorLink(label="Supported image formats", url="https://docs.griptapenodes.com/formats"),
            NodeErrorLink(label="Click", url="javascript:alert(1)"),
        ]
        expected = [NodeErrorLink(label="Supported image formats", url="https://docs.griptapenodes.com/formats")]

        for details in _both_paths(failed_while_running(self._node_error(links=links))):
            assert details.links == expected

    def test_editor_link_survives_on_both_paths(self, failed_while_running: Any) -> None:
        link = NodeErrorLink(label="Add the API key", url="#settings-secrets?filter=MY_KEY")

        for details in _both_paths(failed_while_running(self._node_error(links=[link]))):
            assert details.links == [link]

    def test_other_schemes_are_still_dropped(self) -> None:
        links = [
            NodeErrorLink(label="File", url="file:///etc/passwd"),
            NodeErrorLink(label="Relative", url="settings-secrets"),
            NodeErrorLink(label="Data", url="data:text/html,hi"),
        ]

        details = build_node_error_details(NODE_NAME, self._node_error(links=links))

        assert details.links == []

    def test_links_are_capped_at_three_with_short_labels(self) -> None:
        links = [NodeErrorLink(label="x" * 200, url=f"https://example.com/{i}") for i in range(5)]

        details = build_node_error_details(NODE_NAME, self._node_error(links=links))

        expected_link_count = 3
        expected_label_length = 80
        assert len(details.links) == expected_link_count
        assert all(len(link.label) == expected_label_length for link in details.links)

    def test_attributes_on_other_exceptions_are_ignored(self) -> None:
        exc = ValueError("boom")
        exc.fields = {"looks": "like a NodeError"}  # type: ignore[attr-defined]
        exc.response = {"status": 500}  # type: ignore[attr-defined]

        details = build_node_error_details(NODE_NAME, exc)

        assert details.fields == {}
        assert details.response is None


class TestPrintedResponse:
    """A node that prints the provider's response into its message instead of raising ``NodeError``."""

    def test_labelled_response_moves_to_response_on_both_paths(self, failed_while_running: Any) -> None:
        response_json = {"generation_id": "90db", "status_detail": {"details": "proxy client error"}, "seed": None}
        exc = RuntimeError(f"{NODE_NAME}: Processing failed.\n\nFull API response:\n{response_json}")
        result = failed_while_running(exc)

        for details in _both_paths(result):
            assert details.message == "Processing failed. proxy client error"
            assert details.response == response_json
        assert str(exc) in str(_executor_error(result))

    def test_response_after_a_dash_moves_to_response(self) -> None:
        body = {"error": {"message": "Unsupported value: 'temperature'", "param": "temperature", "code": None}}
        exc = ValueError(f"Agent run failed because of an exception: Error code: 400 - {body}")

        details = build_node_error_details(NODE_NAME, exc)

        assert details.message == (
            "Agent run failed because of an exception: Error code: 400. Unsupported value: 'temperature'"
        )
        assert details.response == body

    @pytest.mark.parametrize(
        "response",
        [
            {"error": {"message": "Temperature is not supported.", "code": "x"}},
            {"message": "Temperature is not supported."},
            {"detail": "Temperature is not supported."},
            {"error": "Temperature is not supported."},
            {"status_detail": {"details": "Temperature is not supported.", "error": "bad_request"}},
            {"status_detail": {"error": "Temperature is not supported."}},
        ],
        ids=["error.message", "message", "detail", "error as text", "status_detail.details", "status_detail.error"],
    )
    def test_the_providers_explanation_stays_in_the_message(self, response: dict[str, Any]) -> None:
        details = build_node_error_details(NODE_NAME, ValueError(f"Request failed: {response}"))

        assert details.message == "Request failed. Temperature is not supported."
        assert details.response == response

    def test_a_response_without_an_explanation_leaves_the_message_short(self) -> None:
        details = build_node_error_details(NODE_NAME, ValueError("Request failed.\nResponse:\n{'status': 'ERRORED'}"))

        assert details.message == "Request failed."

    def test_an_explanation_already_in_the_message_is_not_repeated(self) -> None:
        exc = ValueError("Temperature is not supported: {'message': 'Temperature is not supported'}")

        details = build_node_error_details(NODE_NAME, exc)

        assert details.message == "Temperature is not supported"

    def test_response_after_a_colon_moves_to_response(self) -> None:
        details = build_node_error_details(NODE_NAME, ValueError("Request failed: {'status': 'ERRORED'}"))

        assert details.message == "Request failed"
        assert details.response == {"status": "ERRORED"}

    def test_tuples_in_the_response_become_lists(self) -> None:
        details = build_node_error_details(NODE_NAME, ValueError("Request failed: {'size': (1024, 768)}"))

        assert json.loads(json.dumps(details.response)) == {"size": [1024, 768]}

    def test_braces_before_the_response_are_skipped(self) -> None:
        exc = ValueError("Prompt {subject} failed.\nError details:\n{'reason': 'moderated'}")

        details = build_node_error_details(NODE_NAME, exc)

        assert details.message == "Prompt {subject} failed."
        assert details.response == {"reason": "moderated"}

    def test_crlf_line_endings_are_removed_with_the_label(self) -> None:
        exc = RuntimeError("Processing failed.\r\n\r\nFull API response:\r\n{'status': 'ERRORED'}")

        details = build_node_error_details(NODE_NAME, exc)

        assert details.message == "Processing failed."
        assert details.response == {"status": "ERRORED"}

    @pytest.mark.parametrize("label", ["Full API response:", "Full error details:", "Full response:"])
    def test_label_lines_libraries_use_are_removed(self, label: str) -> None:
        details = build_node_error_details(NODE_NAME, RuntimeError(f"Upload failed.\n\n{label}\n{{'code': 7}}"))

        assert details.message == "Upload failed."
        assert details.response == {"code": 7}

    def test_a_label_of_any_wording_is_removed(self) -> None:
        exc = RuntimeError("Upload failed.\nRaw provider reply:\n{'code': 7}")

        details = build_node_error_details(NODE_NAME, exc)

        assert details.message == "Upload failed."

    def test_a_short_line_without_a_colon_is_kept(self) -> None:
        exc = RuntimeError("Upload failed.\nSee below\n{'code': 7}")

        details = build_node_error_details(NODE_NAME, exc)

        assert details.message == "Upload failed.\nSee below"

    def test_a_short_line_ending_in_a_colon_on_the_same_line_as_the_response_is_kept(self) -> None:
        details = build_node_error_details(NODE_NAME, RuntimeError("Upload failed.\nProvider error: {'code': 7}"))

        assert details.message == "Upload failed.\nProvider error"

    def test_a_dict_right_after_a_word_stays_in_the_message(self) -> None:
        # transformers prints a model config as its class name and then JSON, which can read as a dict.
        message = 'head_dim could not be found in the config:\nCLIPTextConfig {\n  "hidden_size": 512\n}'

        details = build_node_error_details(NODE_NAME, ValueError(message))

        assert details.message == message
        assert details.response is None

    def test_a_provider_response_from_a_dependency_moves_to_response(self) -> None:
        # Raised by huggingface_hub's fal provider, which the diffusers library calls.
        exc = ValueError("Response from fal ai image-segmentation API does not contain an image: {'masks': []}")

        details = build_node_error_details(NODE_NAME, exc)

        assert details.message == "Response from fal ai image-segmentation API does not contain an image"
        assert details.response == {"masks": []}

    def test_a_line_ending_in_a_colon_that_is_not_a_label_is_kept(self) -> None:
        exc = RuntimeError("Generation failed.\nThe provider said for prompt 'cat':\n{'status': 'ERRORED'}")

        details = build_node_error_details(NODE_NAME, exc)

        assert details.message == "Generation failed.\nThe provider said for prompt 'cat'"
        assert details.response == {"status": "ERRORED"}

    def test_a_sentence_ending_in_response_is_kept(self) -> None:
        exc = RuntimeError("Generation failed.\nThe provider returned an unexpected response:\n{'status': 'x'}")

        details = build_node_error_details(NODE_NAME, exc)

        assert details.message == "Generation failed.\nThe provider returned an unexpected response"
        assert details.response == {"status": "x"}

    def test_a_message_that_is_only_a_label_keeps_the_label(self) -> None:
        details = build_node_error_details(NODE_NAME, RuntimeError("Full API response: {'status': 'ERRORED'}"))

        assert details.message == "Full API response"
        assert details.response == {"status": "ERRORED"}

    @pytest.mark.parametrize(
        "message",
        [
            "{'status': 'ERRORED'}",
            "\n{'status': 'ERRORED'}",
            "Request failed: {}",
            "Request failed: {'status': 'ERRORED'} # 3 of 5 retries",
            "a {b} {c} {d} {e} {f} {g} {h} {i}: {'status': 'ERRORED'}",
            f"Request failed: {'{' * 250}'a': 1{'}' * 250}",
            "Request failed: {'status': 'ERRORED'} after 3 retries",
            "Request failed: {'ERRORED', 'FAILED'}",
            "Request failed: {'status': ERRORED}",
            "Request failed: {'data': b'raw'}",
            f"Request failed: {{'image': '{'A' * MAX_RESPONSE_BYTES}'}}",
            f"{'x' * MAX_PRINTED_RESPONSE_MESSAGE_CHARS} {{'status': 'ERRORED'}}",
            "Missing required keys: {'factor'}",
            "Keyword arguments {'foo': 1} are not expected by FluxPipeline and will be ignored.",
            "CUDA out of memory. Tried to allocate 2.00 GiB",
        ],
        ids=[
            "nothing before it",
            "only whitespace before it",
            "empty",
            "a comment after it",
            "too many braces before it",
            "nested too deep",
            "text after it",
            "a set",
            "not a literal",
            "not JSON",
            "over the size cap",
            "message too long",
            "a set from transformers",
            "a dict mid-sentence from diffusers",
            "no dict",
        ],
    )
    def test_message_is_kept_when_there_is_no_response_to_move(self, message: str) -> None:
        details = build_node_error_details(NODE_NAME, ValueError(message))

        assert details.message == message
        assert details.response is None

    def test_attached_response_wins_over_a_printed_one(self) -> None:
        exc = NodeError("Request failed: {'status': 'printed'}", response={"status": "attached"})

        details = build_node_error_details(NODE_NAME, exc)

        assert details.message == "Request failed: {'status': 'printed'}"
        assert details.response == {"status": "attached"}

    def test_dropped_attached_response_is_not_replaced_by_a_printed_one(self) -> None:
        exc = NodeError("Request failed: {'status': 'printed'}", response={"image": "A" * MAX_RESPONSE_BYTES})

        details = build_node_error_details(NODE_NAME, exc)

        assert details.message == "Request failed: {'status': 'printed'}"
        assert details.response is None
        assert details.fields == {RESPONSE_DROPPED_FIELD: "true"}

    def test_node_error_without_a_response_keeps_its_fields_and_gets_the_printed_one(self) -> None:
        exc = NodeError("Request failed: {'status': 'ERRORED'}", fields={"generation_id": "90db"})

        details = build_node_error_details(NODE_NAME, exc)

        assert details.message == "Request failed"
        assert details.response == {"status": "ERRORED"}
        assert details.fields == {"generation_id": "90db"}


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
