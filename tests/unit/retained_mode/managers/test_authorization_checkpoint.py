"""Tests for the authorization checkpoint value types."""

from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
    CheckpointDenial,
    CheckpointFailure,
)


class TestCheckpointDenialReason:
    """`reason` renders the denial as one display string with a safe fallback."""

    def test_joins_failure_details_with_default_separator(self) -> None:
        denial = CheckpointDenial(
            failures=(
                CheckpointFailure(detail="Labs libraries are disabled."),
                CheckpointFailure(detail="Ask your admin to enable them."),
            )
        )
        assert denial.reason() == "Labs libraries are disabled.; Ask your admin to enable them."

    def test_honors_custom_separator(self) -> None:
        denial = CheckpointDenial(
            failures=(
                CheckpointFailure(detail="first"),
                CheckpointFailure(detail="second"),
            )
        )
        assert denial.reason(separator="\n") == "first\nsecond"

    def test_empty_failures_falls_back_to_default(self) -> None:
        # A hook returns None to allow, so empty failures is a contract violation;
        # reason must still yield a coherent sentence rather than an empty string.
        assert CheckpointDenial(failures=()).reason() == "Denied by the license policy."

    def test_empty_failures_honors_custom_default(self) -> None:
        assert CheckpointDenial(failures=()).reason(default="nope") == "nope"
