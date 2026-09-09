"""Tests for BudgetManager.on_get_attribution_context_request.

The attribution header is the one thing E1 produces, so most of these assert on the
*decoded* payload rather than on the manager's internals: that dict is the contract the
Cloud reads, and the Success payload's structured fields have to agree with it.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock, Mock, PropertyMock, patch

import pytest

from griptape_nodes.common.project_templates import ProjectValidationInfo, ProjectValidationStatus
from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
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
from griptape_nodes.retained_mode.managers.project_manager import (
    SYSTEM_DEFAULTS_KEY,
    ProjectChainEntry,
    ProjectInfo,
    ProjectManager,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload

# The complete set of keys allowed on the wire. A new field must consciously
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


def _entry(project_id: str, name: str | None) -> ProjectChainEntry:
    """A chain entry whose id and name never match, so an assertion cannot pass on the wrong one.

    The real type rather than a Mock: `name` is reserved on `Mock`, so `Mock(name="x").name`
    is a Mock rather than a string, and a mocked chain would answer the nameless branch with
    something that is neither a name nor `None`.
    """
    return ProjectChainEntry(id=project_id, name=name)


def _register_project(
    project_manager: ProjectManager,
    project_id: str,
    *,
    name: str | None = None,
    parent_id: str | None = None,
) -> None:
    """Register an id-linked project directly in the in-memory registry.

    Mirrors the helper at `test_project_manager.py:11588`: an explicit `parent_project_id`
    so the chain walk resolves through the registry without touching disk, and a `name` that
    never matches its id, so an assertion cannot pass on the wrong one.
    """
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
    """`tags.project` carries project names, ordered leaf-first.

    The name is the wire value because `tags.project` is the only dimension the Cloud
    matches budgets against, and a budget admin has to be able to author it into a rule.
    """

    def _manager_on(self, project_manager: ProjectManager) -> BudgetManager:
        mock_engine = _mock_engine()
        mock_engine.project_manager = project_manager
        return BudgetManager(MagicMock(), engine=mock_engine)

    def test_system_defaults_never_reaches_the_wire(self) -> None:
        """The Cloud reserves `<system-defaults>` and counts a client copy as degraded.

        Nor does its *name* travel in the sentinel's place -- see the next test.
        """
        project_manager = ProjectManager(Mock(), Mock(), Mock())
        result = _succeed(self._manager_on(project_manager))

        assert "project" not in _tags(result)
        assert result.project_chain == []
        assert SYSTEM_DEFAULTS_KEY not in json.dumps(_decode(result))

    def test_the_default_templates_name_never_stands_in_for_the_sentinel(self) -> None:
        """Skipping `<system-defaults>` is keyed on its id, because it does have a name.

        The rest state is registered with the shipped `DEFAULT_PROJECT_TEMPLATE`, whose name
        is "Default Project" -- a generic string that would collide across every user in an
        org and match any budget rule unlucky enough to be written against it. Filtering on
        the name alone would let it through.
        """
        project_manager = ProjectManager(Mock(), Mock(), Mock())
        project_manager._load_system_defaults()

        result = _succeed(self._manager_on(project_manager))

        assert "project" not in _tags(result)
        assert DEFAULT_PROJECT_TEMPLATE.name not in result.header_value

    def test_system_defaults_is_dropped_from_a_real_chain(self) -> None:
        """Filtered wherever it appears, not only when it is the whole chain."""
        project_manager = ProjectManager(Mock(), Mock(), Mock())
        _register_project(project_manager, "leaf", name="Shot 020", parent_id=SYSTEM_DEFAULTS_KEY)
        project_manager._current_project_id = "leaf"

        result = _succeed(self._manager_on(project_manager))

        assert _tags(result)["project"] == ["Shot 020"]

    @pytest.mark.parametrize(
        "reserved_name",
        [
            SYSTEM_DEFAULTS_KEY,
            f" {SYSTEM_DEFAULTS_KEY} ",
            # Only becomes the reserved value once cut to the 256-char cap.
            SYSTEM_DEFAULTS_KEY + " " * 239 + "x",
        ],
    )
    def test_a_project_actually_named_system_defaults_drops_the_chain(
        self, reserved_name: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A name collision with the Cloud's reserved value costs the whole chain.

        Nothing stops a user typing the reserved string into `name:`. The Cloud discards a
        client copy, which would drop that entry and promote its parent to leaf -- billing a
        real project for spend it never incurred. Neither padding nor length disguises it: the
        far end strips, cuts to its value cap, and strips again before it tests the reserved
        value, so the comparison here runs on that same form.
        """
        project_manager = ProjectManager(Mock(), Mock(), Mock())
        _register_project(project_manager, "grandparent", name="Acme Studios")
        _register_project(project_manager, "leaf", name=reserved_name, parent_id="grandparent")
        project_manager._current_project_id = "leaf"

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            result = _succeed(self._manager_on(project_manager))

        assert "project" not in _tags(result)
        assert result.project_chain == []
        assert "Acme Studios" not in result.header_value
        assert any(record.levelno == logging.WARNING for record in caplog.records)

    def test_nested_chain_carries_every_name_leaf_first(self) -> None:
        """The whole ancestry travels, and nothing but names does."""
        project_manager = ProjectManager(Mock(), Mock(), Mock())
        _register_project(project_manager, "grandparent", name="Acme Studios")
        _register_project(project_manager, "parent", name="Feature Film", parent_id="grandparent")
        _register_project(project_manager, "leaf", name="Shot 020", parent_id="parent")
        project_manager._current_project_id = "leaf"

        result = _succeed(self._manager_on(project_manager))
        decoded = _decode(result)

        assert decoded["tags"]["project"] == ["Shot 020", "Feature Film", "Acme Studios"]
        serialized = json.dumps(decoded)
        for project_id in ("grandparent", "parent", "leaf"):
            assert project_id not in serialized
            assert project_id not in result.header_value

    def test_chain_deeper_than_the_cap_truncates_from_the_leaf(self) -> None:
        """The chain is capped at the Cloud's own MAX_CHAIN_LENGTH, keeping the leaf end."""
        project_manager = ProjectManager(Mock(), Mock(), Mock())
        ids = [f"p{index:02d}" for index in range(40)]
        # ids[0] is the leaf; each entry's parent is the next one along.
        for index, project_id in enumerate(ids):
            parent = ids[index + 1] if index + 1 < len(ids) else None
            _register_project(project_manager, project_id, name=f"Name {project_id}", parent_id=parent)
        project_manager._current_project_id = ids[0]

        names = [f"Name {project_id}" for project_id in ids]
        result = _succeed(self._manager_on(project_manager))

        assert _tags(result)["project"] == names[:32]
        assert result.project_chain == names[:32]
        assert result.chain_truncated is True

    def test_an_unregistered_parent_drops_the_chain_and_never_falls_back_to_its_id(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A nameless ancestor is neither swapped for its id nor quietly cut away.

        Its registry key is not something a budget rule mentions, so emitting it adds one
        unmatchable string. Keeping just the leaf is worse: paths are root-anchored, so
        `["Shot 6"]` presents a nested project as a root and bills an unrelated top-level
        "Shot 6" if the org has one.
        """
        project_manager = ProjectManager(Mock(), Mock(), Mock())
        _register_project(project_manager, "shot-6", name="Shot 6", parent_id="swx")
        project_manager._current_project_id = "shot-6"

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            result = _succeed(self._manager_on(project_manager))

        assert "project" not in _tags(result)
        assert "swx" not in result.header_value
        assert "Shot 6" not in result.header_value
        assert any(record.levelno == logging.WARNING for record in caplog.records)

    @pytest.mark.parametrize("blank_at", ["root", "middle"])
    def test_a_blank_name_drops_the_chain_wherever_it_sits(
        self, blank_at: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A blank name reads as nameless, and position cannot tell it from a failed load.

        `ProjectTemplate.name` has no `min_length` and `get_project_chain` maps any falsy name
        to None, so a loaded blank-named template is indistinguishable from an unresolvable
        one -- including at the root, where it is nameless *and* last. Hence both ends: a rule
        reading "nameless and last means the walk ended" returns `["Shot 020"]` for a project
        three deep and presents it to the Cloud as a root.
        """
        names = {"root": {"acme": "", "mid": "Mid"}, "middle": {"acme": "Acme Studios", "mid": ""}}[blank_at]
        project_manager = ProjectManager(Mock(), Mock(), Mock())
        _register_project(project_manager, "acme", name=names["acme"])
        _register_project(project_manager, "mid", name=names["mid"], parent_id="acme")
        _register_project(project_manager, "shot-020", name="Shot 020", parent_id="mid")
        project_manager._current_project_id = "shot-020"

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            result = _succeed(self._manager_on(project_manager))

        assert "project" not in _tags(result)
        assert "Shot 020" not in result.header_value
        assert any(record.levelno == logging.WARNING for record in caplog.records)

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

        assert _tags(first)["project"] == ["Before"]
        assert _tags(second)["project"] == ["After"]

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
    """Pins the resolver, not a shipping behavior.

    The orchestrator answers every attribution request and has no parent, so production never
    populates this key. It is not evidence that worker spend is joinable to its orchestrator.
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
        mock_engine.project_manager.get_project_chain.return_value = [_entry("leaf", "Leaf")]
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
    """Reduction sheds the cheapest dimensions first, then the chain, then fails."""

    def _manager(self) -> BudgetManager:
        """Short names, so the byte caps in each test below bracket the reduction rung it means to exercise."""
        mock_engine = _mock_engine()
        mock_engine.project_manager.get_project_chain.return_value = [
            _entry("leaf", "Leaf"),
            _entry("parent", "Parent"),
        ]
        mock_engine.context_manager.has_current_workflow.return_value = True
        mock_engine.context_manager.get_current_workflow_name.return_value = "shots/sh020/lighting"
        return BudgetManager(MagicMock(), engine=mock_engine)

    def test_oversized_payload_reduces_to_minimum(self, caplog: pytest.LogCaptureFixture) -> None:
        """Over the cap even without labels: the chain goes whole, and the call still succeeds.

        Not down to the leaf -- paths are root-anchored, so `["Leaf"]` for a nested project
        buys no narrower match than sending nothing and risks billing an unrelated top-level
        "Leaf".
        """
        manager = self._manager()

        with (
            patch.object(budget_manager_module, "_MAX_DECODED_PAYLOAD_BYTES", 80),
            caplog.at_level(logging.WARNING, logger="griptape_nodes"),
        ):
            result = _succeed(manager, node_type="GriptapeProxyImage")

        tags = _tags(result)
        assert "project" not in tags
        assert "workflow" not in tags
        assert "node_type" not in tags
        # The structured fields have to agree with the reduced header, not the pre-encode values.
        assert result.workflow_name is None
        assert result.node_type is None
        assert result.project_chain == []
        # Empty *and* flagged: the chain was shed. Empty and unflagged means no project open.
        assert result.chain_truncated is True
        assert any(record.levelno == logging.WARNING for record in caplog.records)

    def test_labels_are_shed_before_the_chain(self, caplog: pytest.LogCaptureFixture) -> None:
        """An oversized payload gives up the workflow and node type before any ancestor.

        The chain is the only dimension the Cloud matches budgets against, and its paths are
        root-anchored, so an ancestor dropped to make room for an audit-only label costs every
        match the Cloud would have made. Sized so the full payload is over the cap but the
        chain survives without the labels: shedding the chain here would be pure loss.
        """
        manager = self._manager()

        with (
            patch.object(budget_manager_module, "_MAX_DECODED_PAYLOAD_BYTES", 100),
            caplog.at_level(logging.WARNING, logger="griptape_nodes"),
        ):
            result = _succeed(manager, node_type="GriptapeProxyImage")

        tags = _tags(result)
        assert tags["project"] == ["Leaf", "Parent"]
        assert "workflow" not in tags
        assert "node_type" not in tags
        assert result.project_chain == ["Leaf", "Parent"]
        assert result.chain_truncated is False
        assert any(record.levelno == logging.WARNING for record in caplog.records)

    def test_names_are_cut_before_the_chain_is_shed(self, caplog: pytest.LogCaptureFixture) -> None:
        """Past the cap, cutting a name costs a signal that was already gone.

        Sending names whole buys one thing: the far end sees it had to truncate and stops
        matching the chain. A payload over the cap never reaches the far end to carry that,
        so shedding the chain here lands the spend in the default bucket with nothing recorded
        on either side. The cut form matches instead. Run against the real cap, because the
        band this rung exists for starts near 3948 characters and nowhere else.
        """
        mock_engine = _mock_engine()
        mock_engine.project_manager.get_project_chain.return_value = [
            _entry("leaf", "S" * 4200),
            _entry("root", "Acme Studios"),
        ]
        manager = BudgetManager(MagicMock(), engine=mock_engine)

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            result = _succeed(manager)

        assert _tags(result)["project"] == ["S" * 256, "Acme Studios"]
        assert result.project_chain == ["S" * 256, "Acme Studios"]
        # Names were cut, not ancestors dropped. The flag says the latter.
        assert result.chain_truncated is False
        assert any("cut" in record.getMessage() for record in caplog.records)

    def test_a_chain_too_long_even_cut_is_still_shed_whole(self) -> None:
        """The new rung is a rung, not a floor: it does not rescue every oversized chain.

        Thirty-two entries at the value cap encode past 4096 bytes even after cutting, so the
        ladder still falls through to no chain rather than sending a partial one.
        """
        mock_engine = _mock_engine()
        mock_engine.project_manager.get_project_chain.return_value = [
            _entry(f"p{index}", "N" * 300) for index in range(budget_manager_module._MAX_PROJECT_CHAIN_ENTRIES)
        ]
        manager = BudgetManager(MagicMock(), engine=mock_engine)

        result = _succeed(manager)

        assert "project" not in _tags(result)
        assert result.project_chain == []
        assert result.chain_truncated is True

    def test_reduction_of_a_single_entry_chain_does_not_claim_truncation(self) -> None:
        """Reducing an oversized payload cuts nothing when the chain is already one deep.

        `chain_truncated` says ancestors were dropped. A caller passing a huge `node_type`
        can force the reduction step without the chain having anything to lose, and a
        consumer reading the flag to decide whether project attribution is incomplete
        would otherwise get a false positive.
        """
        mock_engine = _mock_engine()
        mock_engine.project_manager.get_project_chain.return_value = [_entry("only", "Only")]
        mock_engine.context_manager.has_current_workflow.return_value = True
        mock_engine.context_manager.get_current_workflow_name.return_value = "shots/sh020"
        manager = BudgetManager(MagicMock(), engine=mock_engine)

        with patch.object(budget_manager_module, "_MAX_DECODED_PAYLOAD_BYTES", 80):
            result = _succeed(manager, node_type="GriptapeProxyImage")

        assert _tags(result)["project"] == ["Only"]
        assert result.project_chain == ["Only"]
        assert result.chain_truncated is False

    def test_unencodable_payload_fails_without_raising(self) -> None:
        """The floor: no usable header value exists, so Success would be a lie."""
        manager = self._manager()

        with patch.object(budget_manager_module, "_MAX_DECODED_PAYLOAD_BYTES", 1):
            result = _dispatch(manager)

        assert isinstance(result, GetAttributionContextResultFailure)
        assert "attribution" in str(result.result_details).lower()


class TestTransmissibility:
    """A value the Cloud cannot store is dropped here rather than sent and discarded there.

    Now that the chain carries names, the plausible offender is a user typing or pasting one:
    a project name is free text from a YAML file, and a pasted newline survives the schema.
    The surrogate case belongs to the path-derived values -- the workflow key, and identifiers
    `os.getenv` decodes with `surrogateescape` -- but a name read off disk can carry one too,
    so both shapes are parametrized against the same filter.
    """

    # A pasted line break in a project name. Legal in YAML, encodes fine here, and dropped
    # by the Cloud's storability check.
    CONTROL_CHAR_NAME = "Acme\nStudios"
    # A name read from a file whose bytes are not valid UTF-8 carries a lone surrogate.
    SURROGATE_NAME = "Renders \udce9"
    # Truthy here, so `get_project_chain` hands it over as a name, but it strips to empty at
    # the far end -- which drops the entry and promotes its parent to leaf.
    WHITESPACE_NAME = "  "
    NBSP_NAME = "\u00a0"

    @pytest.mark.parametrize("bad_name", [CONTROL_CHAR_NAME, SURROGATE_NAME, WHITESPACE_NAME, NBSP_NAME])
    def test_an_untransmissible_name_drops_the_whole_chain(
        self, bad_name: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The chain goes as a unit: a partial chain bills an ancestor for the leaf's spend.

        Budget paths are root-anchored, so a chain missing a link matches nothing either way.
        Dropping only the offending entry would promote its parent to leaf and charge a real
        project for spend it never incurred, which is worse than no attribution at all.
        """
        mock_engine = _mock_engine()
        mock_engine.project_manager.get_project_chain.return_value = [
            _entry("leaf", "Leaf"),
            _entry("parent", bad_name),
        ]
        manager = BudgetManager(MagicMock(), engine=mock_engine)

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            result = _succeed(manager)

        assert "project" not in _tags(result)
        assert result.project_chain == []
        assert any(record.levelno == logging.WARNING for record in caplog.records)

    def test_an_ordinary_human_name_still_travels(self) -> None:
        """The filter targets unstorable bytes, not punctuation -- names are meant to read like names."""
        mock_engine = _mock_engine()
        mock_engine.project_manager.get_project_chain.return_value = [_entry("leaf", "Acme Studios / S02 \u2014 v3")]
        manager = BudgetManager(MagicMock(), engine=mock_engine)

        assert _tags(_succeed(manager))["project"] == ["Acme Studios / S02 \u2014 v3"]

    def test_a_padded_name_travels_in_the_form_the_far_end_stores(self) -> None:
        """Normalize here or the result describes a string budgets never match.

        The Cloud strips what it keeps, so `" Acme Studios "` is stored as `"Acme Studios"`.
        Sending the padded form would leave `project_chain` reporting a value that no budget
        rule can be written against.
        """
        mock_engine = _mock_engine()
        mock_engine.project_manager.get_project_chain.return_value = [_entry("leaf", "  Acme Studios  ")]
        manager = BudgetManager(MagicMock(), engine=mock_engine)

        result = _succeed(manager)

        assert _tags(result)["project"] == ["Acme Studios"]
        assert result.project_chain == ["Acme Studios"]

    def test_a_padded_workflow_key_travels_unstripped(self) -> None:
        """A registry key is an identifier, so it goes out as the engine spells it.

        `derive_registry_key` builds the key from a workspace-relative path, and a directory
        or filename may legally begin or end with a space on macOS and Linux. Trimming it
        would report a string that no longer names any workflow the engine can look up --
        the opposite of the project-name case, where normalizing is what makes the value
        matchable.
        """
        mock_engine = _mock_engine()
        mock_engine.context_manager.has_current_workflow.return_value = True
        mock_engine.context_manager.get_current_workflow_name.return_value = "shots/sh020 /lighting"
        manager = BudgetManager(MagicMock(), engine=mock_engine)

        result = _succeed(manager)

        assert _tags(result)["workflow"] == "shots/sh020 /lighting"
        assert result.workflow_name == "shots/sh020 /lighting"

    def test_a_name_longer_than_the_value_cap_travels_uncut(self) -> None:
        """Cutting is the far end's job, and doing it here would hide that it happened.

        The Cloud truncates an over-long name rather than dropping it, then marks the chain
        mangled and stops matching it against admin-authored paths. Pre-cutting hands it a
        prefix that looks intact, so it matches -- and two sibling projects sharing their
        first 256 characters silently collapse onto one budget with nothing recorded.
        Stripping stays, because the far end strips too and flags nothing when it does.
        """
        mock_engine = _mock_engine()
        mock_engine.project_manager.get_project_chain.return_value = [_entry("leaf", "  " + "S" * 300 + "  ")]
        manager = BudgetManager(MagicMock(), engine=mock_engine)

        result = _succeed(manager)

        assert _tags(result)["project"] == ["S" * 300]
        assert result.project_chain == ["S" * 300]

    def test_an_unstorable_byte_past_the_value_cap_does_not_cost_the_chain(self) -> None:
        """Judge the kept form, or the engine refuses a name the far end would have stored.

        The cut happens before the storability test on the far end, so a control character
        sitting past the cap is gone by the time anything looks at it. Testing the whole
        string here would drop a chain over a byte that never arrives -- and the name still
        travels uncut, because the far end is the one that should decide it was too long.
        """
        mock_engine = _mock_engine()
        mock_engine.project_manager.get_project_chain.return_value = [
            _entry("leaf", "S" * 300 + "\n" + "S" * 20),
            _entry("root", "Acme Studios"),
        ]
        manager = BudgetManager(MagicMock(), engine=mock_engine)

        result = _succeed(manager)

        assert _tags(result)["project"] == ["S" * 300 + "\n" + "S" * 20, "Acme Studios"]

    def test_a_surrogate_past_the_value_cap_falls_back_to_the_cut_form(self) -> None:
        """One name's truncation signal is worth less than every other dimension on the call.

        A lone surrogate survives the strip and cannot be UTF-8 encoded, so sending the uncut
        name would cost the entire header. The cut form drops the byte, which is the trade.
        """
        mock_engine = _mock_engine()
        mock_engine.project_manager.get_project_chain.return_value = [
            _entry("leaf", "S" * 300 + "\udce9"),
            _entry("root", "Acme Studios"),
        ]
        manager = BudgetManager(MagicMock(), engine=mock_engine)

        result = _succeed(manager)

        assert _tags(result)["project"] == ["S" * 256, "Acme Studios"]

    def test_an_untransmissible_workflow_key_is_omitted_without_touching_the_chain(self) -> None:
        """One unusable dimension costs its own key and nothing else."""
        mock_engine = _mock_engine()
        mock_engine.project_manager.get_project_chain.return_value = [_entry("leaf", "Leaf")]
        mock_engine.context_manager.has_current_workflow.return_value = True
        mock_engine.context_manager.get_current_workflow_name.return_value = "shots/sh020\udce9/lighting"
        manager = BudgetManager(MagicMock(), engine=mock_engine)

        result = _succeed(manager)

        tags = _tags(result)
        assert "workflow" not in tags
        assert tags["project"] == ["Leaf"]
        assert result.workflow_name is None

    def test_an_untransmissible_node_type_is_omitted(self) -> None:
        """`node_type` is caller-supplied, so it gets the same check as the engine's own facts."""
        manager = BudgetManager(MagicMock(), engine=_mock_engine())

        result = _succeed(manager, node_type="Griptape\udce9Image")

        assert "node_type" not in _tags(result)
        assert result.node_type is None

    def test_an_unencodable_identifier_costs_its_own_key_and_nothing_else(self) -> None:
        """An identifier gets the same per-field filter every other dimension gets.

        `os.getenv` decodes with `surrogateescape`, so `orchestrator_engine_id` can arrive
        holding a lone surrogate that `str.encode` refuses. Filtering at the read spends one
        key on the documented "could not determine this" degradation; letting it reach the
        encoder costs the whole header, and blames size for a problem that is not size.
        """
        mock_engine = _mock_engine()
        manager = BudgetManager(MagicMock(), engine=mock_engine)

        with patch.dict(os.environ, {"GTN_ORCHESTRATOR_ENGINE_ID": "eng-orch\udce9"}):
            result = _succeed(manager)

        tags = _tags(result)
        assert "orchestrator_engine_id" not in tags
        assert result.orchestrator_engine_id is None
        assert tags["engine_id"] == mock_engine.engine_identity_manager.engine_id


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
