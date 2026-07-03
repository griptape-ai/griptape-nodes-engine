"""Tests for RequestPayload base class broadcast_result behavior."""

import logging

from griptape_nodes.retained_mode.events.base_events import (
    RequestPayload,
    ResultDetail,
    ResultDetails,
    StrictModeViolationDetail,
)
from griptape_nodes.retained_mode.events.event_converter import converter
from griptape_nodes.retained_mode.events.os_events import ReadFileRequest


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
