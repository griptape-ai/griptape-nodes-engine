from __future__ import annotations

import time
from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.events.app_events import (
    EngineInitializationProgress,
    InitializationPhase,
    InitializationStatus,
)
from griptape_nodes.retained_mode.events.base_events import AppEvent

if TYPE_CHECKING:
    from collections.abc import Callable

# Caps progress traffic so it can't queue ahead of the result of the request that triggered the
# scan. Ten updates a second is still smooth for a progress readout.
WORKFLOW_PROGRESS_MIN_INTERVAL_S = 0.1


class WorkflowRegistrationProgressReporter:
    """Sends workflow-registration progress events, at most one per interval.

    Two kinds of event always go out, because clients depend on each one: a failure (clients list
    the failed items) and the scan's final event (clients end their loading state on it). Anything
    else is dropped if the previous event went out less than ``min_interval_s`` ago.
    """

    def __init__(
        self,
        put_event: Callable[[AppEvent], None],
        total: int,
        *,
        min_interval_s: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._put_event = put_event
        self._total = total
        # Resolved here rather than as defaults so the module constant can be patched in tests.
        self._min_interval_s = WORKFLOW_PROGRESS_MIN_INTERVAL_S if min_interval_s is None else min_interval_s
        self._clock = clock or time.monotonic
        self._last_sent_at: float | None = None

    def report(
        self,
        *,
        item_name: str,
        status: InitializationStatus,
        current: int,
        error: str | None = None,
    ) -> None:
        is_final = current >= self._total and status != InitializationStatus.LOADING
        must_send = is_final or status == InitializationStatus.FAILED
        now = self._clock()
        if not must_send and self._last_sent_at is not None and now - self._last_sent_at < self._min_interval_s:
            return
        self._last_sent_at = now
        self._put_event(
            AppEvent(
                payload=EngineInitializationProgress(
                    phase=InitializationPhase.WORKFLOWS,
                    item_name=item_name,
                    status=status,
                    current=current,
                    total=self._total,
                    error=error,
                )
            )
        )
