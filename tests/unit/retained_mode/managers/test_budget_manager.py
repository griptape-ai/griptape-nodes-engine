"""Tests for BudgetManager.on_get_attribution_context_request.

The attribution header is the one thing E1 produces, so most of these assert on the
*decoded* payload rather than on the manager's internals: that dict is the contract the
Cloud reads, and the Success payload's structured fields have to agree with it.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock, Mock, PropertyMock, patch

import pytest

from griptape_nodes.retained_mode.events.budget_events import (
    ATTRIBUTION_HEADER_NAME,
    ATTRIBUTION_SCHEMA_VERSION,
    UNSAVED_WORKFLOW_SENTINEL,
    GetAttributionContextRequest,
    GetAttributionContextResultFailure,
    GetAttributionContextResultSuccess,
)
from griptape_nodes.retained_mode.events.context_events import (
    SetWorkflowContextRequest,
)
from griptape_nodes.retained_mode.managers import budget_manager as budget_manager_module
from griptape_nodes.retained_mode.managers.budget_manager import BudgetManager
from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY, ProjectManager

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload

# The complete set of keys v1 is allowed to put on the wire. A new field must consciously
# update these, which is the point: `node_id` is absent by decision, and `BaseNode.name`
# must never appear under any key.
ALLOWED_ENVELOPE_KEYS = {"v", "tags"}
ALLOWED_TAG_KEYS = {
    "project",
    "workflow",
    "node_type",
    "engine_id",
    "orchestrator_engine_id",
    "session_id",
}


def _decode(result: GetAttributionContextResultSuccess) -> dict[str, Any]:
    """Decode a Success payload's header value back into the dict the Cloud will read."""
    return json.loads(base64.urlsafe_b64decode(result.header_value))


def _tags(result: GetAttributionContextResultSuccess) -> dict[str, Any]:
    """The `tags` object the Cloud reads every dimension out of, or {} when none was sent."""
    return _decode(result).get("tags", {})


def _dispatch(manager: BudgetManager, **kwargs: Any) -> ResultPayload:
    """Invoke the handler directly, bypassing the event bus."""
    return manager.on_get_attribution_context_request(GetAttributionContextRequest(**kwargs))


def _succeed(manager: BudgetManager, **kwargs: Any) -> GetAttributionContextResultSuccess:
    """Invoke the handler and assert it succeeded."""
    result = _dispatch(manager, **kwargs)
    assert isinstance(result, GetAttributionContextResultSuccess)
    return result


def _mock_engine() -> MagicMock:
    """A stand-in engine whose peers answer with JSON-encodable values.

    A bare MagicMock hands back MagicMocks, which `json.dumps` cannot encode -- so every
    peer a resolver reads is given a real value here and overridden per test.
    """
    mock_engine = MagicMock()
    mock_engine.project_manager.get_project_chain.return_value = []
    mock_engine.context_manager.has_current_workflow.return_value = False
    mock_engine.engine_identity_manager.engine_id = "eng-1"
    mock_engine.session_manager.active_session_id = "sess-1"
    return mock_engine


def _register_project(
    project_manager: ProjectManager,
    project_id: str,
    *,
    name: str | None = None,
    parent_id: str | None = None,
) -> None:
    """Register an id-linked project directly in the in-memory registry.

    Mirrors the helper at `test_project_manager.py:11588`: an explicit `parent_project_id`
    so the chain walk resolves through the registry without touching disk, and a distinct
    `name` on each template so the ids-only assertions have something to catch.
    """
    from griptape_nodes.common.project_templates import ProjectValidationInfo, ProjectValidationStatus
    from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
    from griptape_nodes.retained_mode.managers.project_manager import ProjectInfo

    update: dict[str, Any] = {"id": project_id, "parent_project_id": parent_id}
    if name is not None:
        update["name"] = name
    project_manager._successfully_loaded_project_templates[project_id] = ProjectInfo(
        project_id=project_id,
        project_file_path=None,
        project_base_dir=Path("/"),
        template=DEFAULT_PROJECT_TEMPLATE.model_copy(update=update),
        validation=ProjectValidationInfo(status=ProjectValidationStatus.GOOD),
        parsed_situation_schemas={},
        parsed_directory_schemas={},
    )


class TestAttributionPayloadShape:
    """The decoded header is the contract; these pin its shape."""

    @pytest.fixture
    def budget_manager(self) -> BudgetManager:
        return BudgetManager(MagicMock(), engine=_mock_engine())

    def test_request_dispatches_through_the_engine(self, engine: Engine) -> None:
        """The request reaches BudgetManager over the real bus and comes back usable."""
        result = engine.handle_request(GetAttributionContextRequest(node_type="GriptapeProxyImage"))

        assert isinstance(result, GetAttributionContextResultSuccess)
        assert result.header_value
        assert result.header_name == ATTRIBUTION_HEADER_NAME
        assert result.schema_version == ATTRIBUTION_SCHEMA_VERSION

    def test_header_value_decodes_to_documented_payload(self, engine: Engine) -> None:
        """base64url of compact UTF-8 JSON, leaf-first chain, schema version 1."""
        result = engine.handle_request(GetAttributionContextRequest(node_type="GriptapeProxyImage"))
        assert isinstance(result, GetAttributionContextResultSuccess)

        assert _decode(result)["v"] == ATTRIBUTION_SCHEMA_VERSION
        tags = _tags(result)
        assert tags["node_type"] == "GriptapeProxyImage"
        # No project is selected on a bare engine, so the key is absent rather than empty.
        assert tags.get("project", []) == result.project_chain

    def test_padding_round_trips(self, engine: Engine) -> None:
        """Padding is kept so the Cloud can decode without re-padding."""
        result = engine.handle_request(GetAttributionContextRequest())
        assert isinstance(result, GetAttributionContextResultSuccess)

        # Would raise binascii.Error on a stripped-padding value.
        base64.urlsafe_b64decode(result.header_value)
        assert len(result.header_value) % 4 == 0

    def test_payload_key_allowlist(self, engine: Engine) -> None:
        """No key ships that has not been consciously added, and `node_id` is not one of them."""
        result = engine.handle_request(GetAttributionContextRequest(node_type="Anything"))
        assert isinstance(result, GetAttributionContextResultSuccess)

        decoded = _decode(result)
        assert set(decoded) <= ALLOWED_ENVELOPE_KEYS
        assert set(decoded["tags"]) <= ALLOWED_TAG_KEYS
        assert "node_id" not in decoded["tags"]

    def test_structured_fields_agree_with_the_header(self, budget_manager: BudgetManager) -> None:
        """A consumer reading the result must see exactly what the header encodes."""
        mock_engine = cast("MagicMock", budget_manager.engine)
        mock_engine.project_manager.get_project_chain.return_value = []
        mock_engine.context_manager.has_current_workflow.return_value = False
        mock_engine.engine_identity_manager.engine_id = "eng-1"
        mock_engine.session_manager.active_session_id = "sess-1"

        result = _succeed(budget_manager, node_type="NodeT")
        tags = _tags(result)

        assert tags.get("workflow") == result.workflow_name
        assert tags["node_type"] == result.node_type
        assert tags["engine_id"] == result.engine_id
        assert tags["session_id"] == result.session_id


class TestProjectChain:
    """`tags.project` is ordered leaf-first and carries ids only."""

    def _manager_on(self, project_manager: ProjectManager) -> BudgetManager:
        mock_engine = _mock_engine()
        mock_engine.project_manager = project_manager
        return BudgetManager(MagicMock(), engine=mock_engine)

    def test_system_defaults_never_reaches_the_wire(self) -> None:
        """The Cloud reserves `<system-defaults>` and counts a client copy as degraded."""
        project_manager = ProjectManager(Mock(), Mock(), Mock())
        result = _succeed(self._manager_on(project_manager))

        assert "project" not in _tags(result)
        assert result.project_chain == []
        assert SYSTEM_DEFAULTS_KEY not in json.dumps(_decode(result))

    def test_system_defaults_is_dropped_from_a_real_chain(self) -> None:
        """Filtered wherever it appears, not only when it is the whole chain."""
        project_manager = ProjectManager(Mock(), Mock(), Mock())
        _register_project(project_manager, "leaf", name="Shot 020", parent_id=SYSTEM_DEFAULTS_KEY)
        project_manager._current_project_id = "leaf"

        result = _succeed(self._manager_on(project_manager))

        assert _tags(result)["project"] == ["leaf"]

    def test_nested_chain_is_ids_only(self) -> None:
        """Project display names must not reach the payload by any route."""
        project_manager = ProjectManager(Mock(), Mock(), Mock())
        _register_project(project_manager, "grandparent", name="Acme Studios")
        _register_project(project_manager, "parent", name="Feature Film", parent_id="grandparent")
        _register_project(project_manager, "leaf", name="Shot 020", parent_id="parent")
        project_manager._current_project_id = "leaf"

        result = _succeed(self._manager_on(project_manager))
        decoded = _decode(result)

        assert decoded["tags"]["project"] == ["leaf", "parent", "grandparent"]
        serialized = json.dumps(decoded)
        for name in ("Acme Studios", "Feature Film", "Shot 020"):
            assert name not in serialized
            assert name not in result.header_value

    def test_chain_deeper_than_the_cap_truncates_from_the_leaf(self) -> None:
        """The chain is capped at the Cloud's own MAX_CHAIN_LENGTH, keeping the leaf end."""
        project_manager = ProjectManager(Mock(), Mock(), Mock())
        ids = [f"p{index:02d}" for index in range(40)]
        # ids[0] is the leaf; each entry's parent is the next one along.
        for index, project_id in enumerate(ids):
            parent = ids[index + 1] if index + 1 < len(ids) else None
            _register_project(project_manager, project_id, name=f"Name {project_id}", parent_id=parent)
        project_manager._current_project_id = ids[0]

        result = _succeed(self._manager_on(project_manager))

        assert _tags(result)["project"] == ids[:32]
        assert result.project_chain == ids[:32]
        assert result.chain_truncated is True

    def test_unregistered_parent_id_is_still_surfaced(self) -> None:
        """An unresolvable parent ends the chain, and E1 serializes whatever the chain says."""
        project_manager = ProjectManager(Mock(), Mock(), Mock())
        _register_project(project_manager, "shot-6", name="Shot 6", parent_id="swx")
        project_manager._current_project_id = "shot-6"

        result = _succeed(self._manager_on(project_manager))

        assert _tags(result)["project"] == ["shot-6", "swx"]

    def test_second_dispatch_reflects_a_project_switch(self) -> None:
        """The chain is read per invocation, never cached at workflow open."""
        project_manager = ProjectManager(Mock(), Mock(), Mock())
        _register_project(project_manager, "before", name="Before")
        _register_project(project_manager, "after", name="After")
        manager = self._manager_on(project_manager)

        project_manager._current_project_id = "before"
        first = _succeed(manager)
        project_manager._current_project_id = "after"
        second = _succeed(manager)

        assert _tags(first)["project"] == ["before"]
        assert _tags(second)["project"] == ["after"]

    def test_empty_chain_omits_the_project_key(self) -> None:
        """`[]` would assert 'belongs to zero projects', which is a claim we cannot make."""
        manager = BudgetManager(MagicMock(), engine=_mock_engine())
        result = _succeed(manager)

        assert "project" not in _tags(result)
        # The other dimensions still ship; only the chain is unknown.
        assert "engine_id" in _tags(result)


class TestWorkflowName:
    """An absent `workflow` means unknown; `<unsaved>` means scratch. They differ."""

    def test_unsaved_workflow_reports_the_sentinel(self, engine: Engine) -> None:
        """Never the per-session `unsaved:<uuid4>` key, which has unbounded cardinality."""
        set_result = engine.handle_request(SetWorkflowContextRequest(display_name="Untitled"))
        assert set_result.succeeded()
        try:
            result = engine.handle_request(GetAttributionContextRequest())
            assert isinstance(result, GetAttributionContextResultSuccess)

            decoded = _decode(result)
            assert decoded["tags"]["workflow"] == UNSAVED_WORKFLOW_SENTINEL
            assert result.workflow_name == UNSAVED_WORKFLOW_SENTINEL
            # The uuid must not leak by any other route, encoded or decoded.
            assert "unsaved:" not in json.dumps(decoded)
            assert "unsaved:" not in base64.urlsafe_b64decode(result.header_value).decode("utf-8")
        finally:
            while engine.context_manager.has_current_workflow():
                engine.context_manager.pop_workflow()

    def test_saved_workflow_reports_its_registry_key(self, engine: Engine) -> None:
        """A saved workflow's key is the workspace-relative path minus extension."""
        engine.context_manager.push_workflow("shots/sh020/lighting")
        try:
            result = engine.handle_request(GetAttributionContextRequest())
            assert isinstance(result, GetAttributionContextResultSuccess)

            workflow = _tags(result)["workflow"]
            assert workflow == "shots/sh020/lighting"
            assert not workflow.startswith("unsaved:")
        finally:
            engine.context_manager.pop_workflow()

    def test_missing_workflow_context_omits_workflow(self, engine: Engine) -> None:
        """No workflow pushed -> the key is absent, which is distinct from the scratch bucket."""
        assert not engine.context_manager.has_current_workflow()

        result = engine.handle_request(GetAttributionContextRequest())
        assert isinstance(result, GetAttributionContextResultSuccess)

        assert "workflow" not in _tags(result)
        assert result.workflow_name is None


class TestOrchestratorEngineId:
    """Pins the resolver, NOT a shipping behavior.

    Under the forwarding design the orchestrator answers every attribution request, and the
    orchestrator has no parent, so production never populates this key. Nobody should later
    read these tests as evidence that worker spend is joinable to its orchestrator: it is
    not, and it does not need to be, because budgets match on the project chain alone.
    """

    @pytest.fixture
    def budget_manager(self) -> BudgetManager:
        return BudgetManager(MagicMock(), engine=_mock_engine())

    def test_orchestrator_engine_id_present_when_the_env_var_is_set(
        self, budget_manager: BudgetManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GTN_ORCHESTRATOR_ENGINE_ID", "eng-orch")

        result = _succeed(budget_manager)

        assert _tags(result)["orchestrator_engine_id"] == "eng-orch"
        assert result.orchestrator_engine_id == "eng-orch"

    def test_orchestrator_engine_id_absent_on_the_orchestrator(
        self, budget_manager: BudgetManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GTN_ORCHESTRATOR_ENGINE_ID", raising=False)

        result = _succeed(budget_manager)

        assert "orchestrator_engine_id" not in _tags(result)
        assert result.orchestrator_engine_id is None


class TestDegradation:
    """The caller is about to spend money: degrade to a missing key, never raise."""

    def _manager(self) -> BudgetManager:
        mock_engine = _mock_engine()
        mock_engine.project_manager.get_project_chain.return_value = [Mock(id="leaf")]
        mock_engine.context_manager.has_current_workflow.return_value = True
        mock_engine.context_manager.get_current_workflow_name.return_value = "shots/sh020"
        return BudgetManager(MagicMock(), engine=mock_engine)

    @pytest.mark.parametrize(
        ("break_field", "absent_key"),
        [
            ("project_chain", "project"),
            ("workflow", "workflow"),
            ("engine_id", "engine_id"),
            ("session_id", "session_id"),
        ],
    )
    def test_peer_failure_degrades_one_field_not_the_header(
        self, break_field: str, absent_key: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """One unhappy peer costs one key, not the whole attribution."""
        manager = self._manager()
        mock_engine = cast("MagicMock", manager.engine)
        boom = RuntimeError("peer exploded")

        if break_field == "project_chain":
            mock_engine.project_manager.get_project_chain.side_effect = boom
        elif break_field == "workflow":
            mock_engine.context_manager.get_current_workflow_name.side_effect = boom
        elif break_field == "engine_id":
            type(mock_engine.engine_identity_manager).engine_id = PropertyMock(side_effect=boom)
        elif break_field == "session_id":
            type(mock_engine.session_manager).active_session_id = PropertyMock(side_effect=boom)

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            result = _succeed(manager)

        assert absent_key not in _tags(result)
        assert any(record.levelno == logging.WARNING for record in caplog.records)

    def test_no_workflow_context_is_not_an_error(self) -> None:
        """`has_current_workflow()` returning False is an ordinary state, not a degradation."""
        manager = self._manager()
        cast("MagicMock", manager.engine).context_manager.has_current_workflow.return_value = False

        result = _succeed(manager)

        assert "workflow" not in _tags(result)


class TestSizeCap:
    """One reduction step and a floor, not a ladder."""

    def _manager(self) -> BudgetManager:
        mock_engine = _mock_engine()
        mock_engine.project_manager.get_project_chain.return_value = [Mock(id="leaf"), Mock(id="parent")]
        mock_engine.context_manager.has_current_workflow.return_value = True
        mock_engine.context_manager.get_current_workflow_name.return_value = "shots/sh020/lighting"
        return BudgetManager(MagicMock(), engine=mock_engine)

    def test_oversized_payload_reduces_to_minimum(self, caplog: pytest.LogCaptureFixture) -> None:
        """Over the cap: drop workflow and node type, keep the leaf, still succeed."""
        manager = self._manager()

        with (
            patch.object(budget_manager_module, "_MAX_DECODED_PAYLOAD_BYTES", 80),
            caplog.at_level(logging.WARNING, logger="griptape_nodes"),
        ):
            result = _succeed(manager, node_type="GriptapeProxyImage")

        tags = _tags(result)
        assert tags["project"] == ["leaf"]
        assert "workflow" not in tags
        assert "node_type" not in tags
        # The structured fields have to agree with the reduced header, not the pre-encode values.
        assert result.workflow_name is None
        assert result.node_type is None
        assert result.project_chain == ["leaf"]
        assert result.chain_truncated is True
        assert any(record.levelno == logging.WARNING for record in caplog.records)

    def test_unencodable_payload_fails_without_raising(self) -> None:
        """The floor: no usable header value exists, so Success would be a lie."""
        manager = self._manager()

        with patch.object(budget_manager_module, "_MAX_DECODED_PAYLOAD_BYTES", 1):
            result = _dispatch(manager)

        assert isinstance(result, GetAttributionContextResultFailure)
        assert "attribution" in str(result.result_details).lower()


class TestConfidentiality:
    """The header value is descriptive, not secret -- but it still stays out of the logs."""

    def test_request_does_not_broadcast(self) -> None:
        """`broadcast_result=False` keeps the Success payload off the WebSocket feed."""
        assert GetAttributionContextRequest().broadcast_result is False

    def test_broadcast_result_stays_keyword_only(self) -> None:
        """Pins `field(..., kw_only=True)` against a future bare redeclaration.

        The base payload is `kw_only=True` but this subclass is a plain `@dataclass`, so a
        bare `broadcast_result = False` would re-register it as the second positional
        parameter -- and a caller passing `node_type` positionally could then flip
        broadcasting back on by accident.
        """
        assert GetAttributionContextRequest("GriptapeProxyImage").node_type == "GriptapeProxyImage"
        with pytest.raises(TypeError):
            GetAttributionContextRequest("GriptapeProxyImage", True)  # type: ignore[misc]

    def test_header_value_is_never_logged(self, engine: Engine, caplog: pytest.LogCaptureFixture) -> None:
        """Neither the log stream nor `result_details`, which is logged as well as broadcast."""
        with caplog.at_level(logging.DEBUG, logger="griptape_nodes"):
            result = engine.handle_request(GetAttributionContextRequest(node_type="GriptapeProxyImage"))
        assert isinstance(result, GetAttributionContextResultSuccess)

        assert result.header_value not in caplog.text
        assert result.header_value not in str(result.result_details)

    def test_node_label_never_appears(self, engine: Engine) -> None:
        """`BaseNode.name` is user-authored and forbidden; no key named `name` at any depth."""
        result = engine.handle_request(GetAttributionContextRequest(node_type="GriptapeProxyImage"))
        assert isinstance(result, GetAttributionContextResultSuccess)

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    assert key != "name"
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(_decode(result))


class TestWiring:
    """The manager exists on every engine. Worker forwarding is covered in tests/unit/worker/."""

    def test_engine_exposes_the_budget_manager(self, engine: Engine) -> None:
        assert isinstance(engine.budget_manager, BudgetManager)
        assert engine.BudgetManager() is engine.budget_manager
