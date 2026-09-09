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

    def test_a_project_actually_named_system_defaults_drops_the_chain(self, caplog: pytest.LogCaptureFixture) -> None:
        """A name collision with the Cloud's reserved value costs the whole chain.

        Nothing stops a user typing the reserved string into `name:`. The Cloud discards a
        client copy, which would drop that entry and promote its parent to leaf -- billing a
        real project for spend it never incurred.
        """
        project_manager = ProjectManager(Mock(), Mock(), Mock())
        _register_project(project_manager, "grandparent", name="Acme Studios")
        _register_project(project_manager, "leaf", name=SYSTEM_DEFAULTS_KEY, parent_id="grandparent")
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

    def test_an_unregistered_parent_ends_the_chain_without_falling_back_to_its_id(self) -> None:
        """A nameless ancestor is dropped, not swapped for its id.

        `get_project_chain` still surfaces an unresolvable parent, but with no template it
        has no name -- and its registry key is not something a budget rule mentions. Emitting
        that would put one unmatchable string in the list; the leaf alone at least matches a
        rule written against the leaf.
        """
        project_manager = ProjectManager(Mock(), Mock(), Mock())
        _register_project(project_manager, "shot-6", name="Shot 6", parent_id="swx")
        project_manager._current_project_id = "shot-6"

        result = _succeed(self._manager_on(project_manager))

        assert _tags(result)["project"] == ["Shot 6"]
        assert "swx" not in result.header_value

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
        """Over the cap: drop workflow and node type, keep the leaf, still succeed."""
        manager = self._manager()

        with (
            patch.object(budget_manager_module, "_MAX_DECODED_PAYLOAD_BYTES", 80),
            caplog.at_level(logging.WARNING, logger="griptape_nodes"),
        ):
            result = _succeed(manager, node_type="GriptapeProxyImage")

        tags = _tags(result)
        assert tags["project"] == ["Leaf"]
        assert "workflow" not in tags
        assert "node_type" not in tags
        # The structured fields have to agree with the reduced header, not the pre-encode values.
        assert result.workflow_name is None
        assert result.node_type is None
        assert result.project_chain == ["Leaf"]
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

    @pytest.mark.parametrize("bad_name", [CONTROL_CHAR_NAME, SURROGATE_NAME])
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

    def test_an_unencodable_identifier_degrades_instead_of_raising(self) -> None:
        """The encoder floor: identifiers are not filtered individually, so it must not raise.

        `os.getenv` decodes with `surrogateescape`, so `orchestrator_engine_id` can arrive
        holding a lone surrogate. Raising out of the handler would return `GenericResultFailure`,
        which ignores `failure_log_level` and would log an ERROR with a traceback on every
        metered call rather than degrading quietly.
        """
        mock_engine = _mock_engine()
        manager = BudgetManager(MagicMock(), engine=mock_engine)

        with patch.dict(os.environ, {"GTN_ORCHESTRATOR_ENGINE_ID": "eng-orch\udce9"}):
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
