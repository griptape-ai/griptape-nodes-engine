"""Every success result says how it reports whether it changed the workflow."""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import griptape_nodes.retained_mode.events as events_pkg
from griptape_nodes.retained_mode.events.base_events import (
    ResultPayloadSuccess,
    WorkflowAlteredMixin,
    WorkflowAlteredReportedMixin,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueResultSuccess
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry

_ALTERED_STATE_MIXINS = (WorkflowAlteredMixin, WorkflowNotAlteredMixin, WorkflowAlteredReportedMixin)


def _registered_success_results() -> list[type[ResultPayloadSuccess]]:
    for module_info in pkgutil.iter_modules(events_pkg.__path__, f"{events_pkg.__name__}."):
        # A script that writes a schema file when imported.
        if module_info.name.endswith(".generate_request_payload_schemas"):
            continue
        importlib.import_module(module_info.name)
    return sorted(
        (
            payload
            for payload in PayloadRegistry.get_registry().values()
            if isinstance(payload, type) and issubclass(payload, ResultPayloadSuccess)
        ),
        key=lambda payload: payload.__name__,
    )


@pytest.mark.parametrize("result_type", _registered_success_results(), ids=lambda result_type: result_type.__name__)
def test_success_result_declares_how_it_reports_altered_state(result_type: type[ResultPayloadSuccess]) -> None:
    """A result with no mixin would report no change whenever its handler leaves the field out."""
    declared = [mixin.__name__ for mixin in _ALTERED_STATE_MIXINS if issubclass(result_type, mixin)]

    assert len(declared) == 1, (
        f"{result_type.__name__} must extend exactly one of WorkflowAlteredMixin, WorkflowNotAlteredMixin "
        f"or WorkflowAlteredReportedMixin, so a handler can't report no change by leaving the field out. "
        f"It extends {declared or 'none'}."
    )


def test_a_reported_result_cannot_be_built_without_saying_whether_it_altered_the_workflow() -> None:
    """Leaving the field out of a result whose handler decides it fails rather than reporting no change."""
    with pytest.raises(TypeError, match="altered_workflow_state"):
        SetParameterValueResultSuccess(finalized_value=1, data_type="int", result_details="Set.")  # pyright: ignore[reportCallIssue]
