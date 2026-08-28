"""Fixtures for the scheduler tests. Shared non-fixture scaffolding lives in scheduler_stubs.py."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from griptape_nodes.machines.parallel_resolution import ExecuteDagState

if TYPE_CHECKING:
    from griptape_nodes.machines.dag_builder import DagNode


@pytest.fixture(autouse=True)
def _no_cluster_derivation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub cluster derivation for every scheduler test.

    It reads real node metadata and connections, which these tests stub out; they care
    about who drives the machine and when nodes dispatch, never about placement.
    """
    monkeypatch.setattr(ExecuteDagState, "_compute_heavy_clusters", MagicMock())


@pytest.fixture
def held_node_execution(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    """These reach into the full engine (events, connections, library registry).

    Stub them so these stay focused scheduler tests: they care about when a node is
    dispatched, not what executing it does. `execute_node` blocks on the returned event
    so a dispatched node stays PROCESSING long enough to assert on, instead of
    completing in the same scheduler pass that started it.
    """
    monkeypatch.setattr(ExecuteDagState, "handle_done_nodes", AsyncMock())
    monkeypatch.setattr(ExecuteDagState, "collect_values_from_upstream_nodes", AsyncMock())

    hold = asyncio.Event()

    async def _hold_until_released(_engine: object, _dag_node: DagNode, _context: object = None) -> None:
        await hold.wait()

    monkeypatch.setattr(ExecuteDagState, "execute_node", _hold_until_released)
    return hold
