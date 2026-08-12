"""Helper for reproducing the one condition under which construction defers a bus request.

The model-dropdown components skip their policy / download-status queries exactly when
``reentrant_bus_in_init_would_report()`` is True: a node ``__init__`` on the stack AND a
strict-mode scope open, which in production is the worker's schema probe or node execution.
Both halves are required, so a test that wants the deferral has to set up both -- and a test
that wants the ordinary path (an editor drop, a workflow load) sets up only the first.

Lives in one module so the three component test files pin the same condition. Setting the
constructing flag alone here would silently stop exercising deferral the moment the shared
predicate changes.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from griptape_nodes.common.strict_mode import STRICT_MODE, StrictModeScopeKind
from griptape_nodes.node_library.library_registry import LibraryRegistry

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def constructing_under_probe(subject: str = "SomeNode") -> Iterator[None]:
    """Construct as the worker's schema probe does: LOAD_PROBE scope + constructing flag.

    ``is_worker=True`` matches the probe, which only ever runs on a worker.
    """
    with (
        STRICT_MODE.open_scope(
            kind=StrictModeScopeKind.LOAD_PROBE,
            subject=subject,
            library_name="test_library",
            is_worker=True,
        ),
        LibraryRegistry.constructing_node(),
    ):
        yield
