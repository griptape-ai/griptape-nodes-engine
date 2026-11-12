"""The message a user sees when running a node fails while its run is starting or being joined."""

import pytest

from griptape_nodes.retained_mode.managers.node_manager import NodeManager


def _wrapped(error: Exception) -> RuntimeError:
    """What a run that fails before it begins re-raises: `raise RuntimeError(e) from e`."""
    wrapper = RuntimeError(error)
    wrapper.__cause__ = error
    return wrapper


class TestResolveFailureDetails:
    def test_an_unexpected_error_is_named_by_its_type(self) -> None:
        details = NodeManager._resolve_failure_details("B", KeyError("B"))

        assert details == "Attempted to run 'B'. Failed with an unexpected KeyError: 'B'"

    def test_an_unexpected_error_wrapped_by_a_cold_start_is_still_named_by_its_type(self) -> None:
        details = NodeManager._resolve_failure_details("B", _wrapped(KeyError("B")))

        assert details == "Attempted to run 'B'. Failed with an unexpected KeyError: 'B'"

    @pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
    def test_a_message_written_for_the_user_passes_through_once(self, error_type: type[Exception]) -> None:
        message = "Attempted to run 'B' as part of the current run. Try again once the run ends."

        assert NodeManager._resolve_failure_details("B", error_type(message)) == message
        assert NodeManager._resolve_failure_details("B", _wrapped(error_type(message))) == message
