"""Completeness guard for the undo policy the domain managers declare to UndoManager.

Undo recording is opt-in: only request types a domain declares via ``register_undoable`` become
snapshot points. That default matters because a flow snapshot models only flow contents (nodes,
connections, parameter values, metadata, locks). A request that mutates state outside the flow --
variables, libraries, MCP servers, the workflow registry -- produces identical before and after
snapshots, so recording it would push an undo entry that consumes a stack slot, clears the redo
stack, and reverts nothing.

Asserting against a hand-copied expected list would only catch drift between two copies. So these
tests derive from the engine itself: they introspect the EventManager's routing table to find which
manager owns each declared type, and resolve each request's ``<Name>ResultSuccess`` to check whether
it actually reports ``altered_workflow_state``. What they enforce:

- every declared type can actually commit (its success result alters workflow state),
- every declared type belongs to a domain the snapshot models,
- no altering request outside those domains is declared,
- every altering request inside those domains is either declared or explicitly acknowledged as
  intentionally not undoable.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.retained_mode.engine import current_engine
from griptape_nodes.retained_mode.events.base_events import WorkflowAlteredMixin
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, DeleteNodeRequest
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.undo import snapshot as snapshot_module
from griptape_nodes.retained_mode.managers.undo.snapshot import SnapshotRecordingSession

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.retained_mode.events.base_events import RequestPayload

# The domains whose state a flow snapshot models, and which therefore declare undoable requests.
_SNAPSHOT_DOMAIN_MANAGERS = ("FlowManager", "NodeManager", "ObjectManager")

# Altering requests inside the snapshot domains that are deliberately NOT undoable: execution and
# runtime triggers. They alter workflow state, but undo is about editing, not running. Listing them
# here is the acknowledgment that they were considered; an altering request in those domains that is
# neither declared undoable nor named here fails test_snapshot_domain_altering_requests_are_triaged.
_ACKNOWLEDGED_NOT_UNDOABLE: frozenset[str] = frozenset(
    {
        # Flow execution triggers.
        "StartFlowRequest",
        "StartFlowFromNodeRequest",
        "StartLocalSubflowRequest",
        "SingleNodeStepRequest",
        "SingleExecutionStepRequest",
        "ContinueExecutionStepRequest",
        "CancelFlowRequest",
        "UnresolveFlowRequest",
        # Node execution / runtime state.
        "ResolveNodeRequest",
        "UnresolveNodeRequest",
        # Moves between flows: a snapshot records only the top-level flow's direct contents, so
        # undoing a move into or out of a subflow (node groups use one) would recreate the node
        # alongside the original instead of moving it back.
        "MoveNodeToNewFlowRequest",
        "AddNodesToNodeGroupRequest",
        "RemoveNodeFromNodeGroupRequest",
        # Parameter structure: restore reconciles a survivor node's values, metadata, lock, and
        # connections, not the set of parameters it carries, so undoing one would report success and
        # leave the parameter as it is.
        "AddParameterToNodeRequest",
        "RemoveParameterFromNodeRequest",
        "RenameParameterRequest",
        "AlterParameterDetailsRequest",
        "MigrateParameterRequest",
        "ReorderParameterListItemRequest",
        "AddParameterGroupToNodeRequest",
        "AlterParameterGroupDetailsRequest",
        # Replaces the whole object graph; handled as a history-clearing lifecycle event instead.
        "ClearAllObjectStateRequest",
    }
)

# Stands in for a captured snapshot in the mechanism test, which only cares whether a frame opened.
_STUB_SNAPSHOT = object()


def _owning_manager_name(request_type: type[RequestPayload]) -> str | None:
    """Name of the manager class the EventManager dispatches this request type to."""
    handler = GriptapeNodes.EventManager()._request_type_to_manager.get(request_type)
    owner = getattr(handler, "__self__", None)
    if owner is None:
        return None
    return type(owner).__name__


def _routed_request_types(manager_name: str) -> list[type[RequestPayload]]:
    """Every request type the EventManager dispatches to a bound method of the named manager."""
    routing = GriptapeNodes.EventManager()._request_type_to_manager
    return [request_type for request_type in routing if _owning_manager_name(request_type) == manager_name]


def _alters_workflow_state(request_type: type[RequestPayload]) -> bool:
    """Whether this request's success result forces ``altered_workflow_state``, i.e. can commit a batch.

    The success type is resolved by the engine-wide ``<Name>Request`` -> ``<Name>ResultSuccess``
    naming convention within the request's own module. An unresolvable one is reported by
    test_result_success_types_are_resolvable rather than skipped silently, so this derivation cannot
    quietly degrade into a vacuous one.
    """
    result_type = _result_success_type(request_type)
    if result_type is None:
        return False
    return issubclass(result_type, WorkflowAlteredMixin)


def _result_success_type(request_type: type[RequestPayload]) -> type | None:
    module = sys.modules[request_type.__module__]
    success_name = request_type.__name__.removesuffix("Request") + "ResultSuccess"
    return getattr(module, success_name, None)


class TestUndoPolicy:
    @pytest.fixture
    def recording_session(self) -> Iterator[SnapshotRecordingSession]:
        """Rebuild the singleton and return the SnapshotRecordingSession the UndoManager wired up.

        No library registration is needed: these tests inspect the declared policy and never create
        a node.
        """
        from griptape_nodes.node_library.library_registry import LibraryRegistry
        from griptape_nodes.utils.metaclasses import SingletonMeta

        SingletonMeta._instances.clear()
        LibraryRegistry._clear()
        griptape_nodes = GriptapeNodes()
        recording = griptape_nodes.UndoManager()._recording
        assert isinstance(recording, SnapshotRecordingSession)
        yield recording
        SingletonMeta._instances.clear()
        LibraryRegistry._clear()

    # ---------- What is declared undoable ----------

    def test_declared_undoable_requests_can_actually_commit(self, recording_session: SnapshotRecordingSession) -> None:
        """A declared type whose result never alters workflow state can never commit, so declaring it lies.

        Such a declaration silently does nothing: the frame opens, pays for a whole-flow capture, and
        finalizes without committing. Either the request should report its alteration or it should not
        be declared.
        """
        declared = recording_session._undoable_labels
        assert declared, "no undoable requests declared at all; the wiring is broken"

        inert = sorted(request_type.__name__ for request_type in declared if not _alters_workflow_state(request_type))
        assert inert == []

    def test_declared_operations_have_a_human_name(self, recording_session: SnapshotRecordingSession) -> None:
        """Every declared type carries the name of its operation, which becomes the undo entry's label.

        The name is what the editor shows for "undo X", so a blank or placeholder one leaves the user
        unable to tell what a stack entry will revert.
        """
        unnamed = sorted(
            request_type.__name__
            for request_type, label in recording_session._undoable_labels.items()
            if not label.strip() or label == "Edit"
        )
        assert unnamed == []

    def test_declared_undoable_requests_belong_to_snapshot_domains(
        self, recording_session: SnapshotRecordingSession
    ) -> None:
        """Only domains a flow snapshot models may declare undoable requests.

        Declaring a request from any other domain records an entry whose before and after snapshots
        are identical, so undoing it would burn a stack slot and revert nothing.
        """
        out_of_scope = sorted(
            f"{request_type.__name__} ({_owning_manager_name(request_type)})"
            for request_type in recording_session._undoable_labels
            if _owning_manager_name(request_type) not in _SNAPSHOT_DOMAIN_MANAGERS
        )
        assert out_of_scope == []

    def test_altering_requests_outside_snapshot_domains_are_not_undoable(
        self, recording_session: SnapshotRecordingSession
    ) -> None:
        """No altering request from a non-snapshot domain may be undoable, however it got declared.

        The inverse of the test above, stated over the engine's whole routing table rather than over
        the declared set, so a request reachable through some other wiring path is caught too.
        """
        routing = GriptapeNodes.EventManager()._request_type_to_manager
        leaked = sorted(
            f"{request_type.__name__} ({_owning_manager_name(request_type)})"
            for request_type in routing
            if _owning_manager_name(request_type) not in _SNAPSHOT_DOMAIN_MANAGERS
            and _alters_workflow_state(request_type)
            and request_type in recording_session._undoable_labels
        )
        assert leaked == []

    # ---------- Completeness within the snapshot domains ----------

    def test_snapshot_domain_altering_requests_are_triaged(self, recording_session: SnapshotRecordingSession) -> None:
        """Every altering request in a snapshot domain must be declared undoable or acknowledged as not.

        A request that is neither is an edit nobody decided on: it silently will not be undoable, and
        the omission is invisible without this check.
        """
        candidates = [
            request_type
            for manager_name in _SNAPSHOT_DOMAIN_MANAGERS
            for request_type in _routed_request_types(manager_name)
            if _alters_workflow_state(request_type)
        ]
        assert candidates, "derivation found no altering requests at all; the introspection is broken"

        untriaged = sorted(
            request_type.__name__
            for request_type in candidates
            if request_type not in recording_session._undoable_labels
            and request_type.__name__ not in _ACKNOWLEDGED_NOT_UNDOABLE
        )
        assert untriaged == []

    def test_declared_and_acknowledged_sets_are_disjoint(self, recording_session: SnapshotRecordingSession) -> None:
        """A type cannot be both declared undoable and acknowledged as intentionally not undoable.

        Without this, re-declaring an acknowledged type (undoing a deliberate exclusion) leaves every
        other policy test passing: the type is no longer "untriaged", and its stale acknowledgment
        entry is simply never consulted.
        """
        declared_names = {request_type.__name__ for request_type in recording_session._undoable_labels}
        assert sorted(declared_names & _ACKNOWLEDGED_NOT_UNDOABLE) == []

    def test_acknowledged_not_undoable_types_are_not_stale(self, recording_session: SnapshotRecordingSession) -> None:  # noqa: ARG002
        """The acknowledgment set must not name types that no longer alter state in a snapshot domain.

        Without this, a request that stops altering (or is renamed, or moves domains) leaves a stale
        entry that would mask a genuinely untriaged type sharing its name later.
        """
        derived_names = {
            request_type.__name__
            for manager_name in _SNAPSHOT_DOMAIN_MANAGERS
            for request_type in _routed_request_types(manager_name)
            if _alters_workflow_state(request_type)
        }
        assert sorted(_ACKNOWLEDGED_NOT_UNDOABLE - derived_names) == []

    def test_result_success_types_are_resolvable(self, recording_session: SnapshotRecordingSession) -> None:  # noqa: ARG002
        """Every snapshot-domain request must expose a resolvable success type, or the derivation goes blind.

        A request whose ``<Name>ResultSuccess`` cannot be found looks non-altering to the checks
        above, so it is surfaced here instead of being silently dropped.
        """
        unresolvable = sorted(
            request_type.__name__
            for manager_name in _SNAPSHOT_DOMAIN_MANAGERS
            for request_type in _routed_request_types(manager_name)
            if _result_success_type(request_type) is None
        )
        assert unresolvable == []

    # ---------- Mechanism: register_undoable gates frame opening ----------

    def test_only_registered_undoable_types_open_a_frame(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """register_undoable is the mechanism the declared policy feeds into.

        A registered type must open a frame; an unregistered one must not, even though both are
        user-initiated and both alter workflow state. Built against a scratch session with stub
        callables and a stubbed capture, so this does not depend on a live flow.
        """
        monkeypatch.setattr(snapshot_module, "capture_workflow_snapshot", lambda _engine: _STUB_SNAPSHOT)
        session = SnapshotRecordingSession(
            engine=current_engine(),
            is_replaying=lambda: False,
            commit_batch=lambda _batch: None,
            invalidate_history=lambda: None,
        )
        session.register_undoable({CreateNodeRequest: "Create node"})

        opened = session.begin_request_dispatch(CreateNodeRequest(node_type="Probe"), "some-request-id")
        assert opened is not None
        assert opened.opened
        session.end_request_dispatch(opened, CreateNodeRequest(node_type="Probe"), None)

        assert session.begin_request_dispatch(DeleteNodeRequest(), "some-request-id") is None
