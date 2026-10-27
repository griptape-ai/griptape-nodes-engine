"""Workflow registry helpers for tests."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.retained_mode.engine import Engine


@contextmanager
def empty_workflow_registry(engine: Engine) -> Iterator[None]:
    """Run with the engine's workflow registry empty, restoring what it held afterwards."""
    registry = engine.workflow_registry
    with (
        patch.dict(registry._by_id, {}, clear=True),
        patch.dict(registry._id_by_path, {}, clear=True),
        patch.dict(registry._id_by_name, {}, clear=True),
    ):
        yield
