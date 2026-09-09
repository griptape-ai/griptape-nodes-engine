"""Tests for BudgetManager.on_get_attribution_context_request.

The attribution header is the one thing E1 produces, so most of these assert on the
*decoded* payload rather than on the manager's internals: that dict is the contract the
Cloud reads, and the Success payload's `project_chain` has to agree with it.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock, Mock

import pytest

from griptape_nodes.common.project_templates import ProjectValidationInfo, ProjectValidationStatus
from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
from griptape_nodes.retained_mode.events.base_events import ResultDetails
from griptape_nodes.retained_mode.events.budget_events import (
    ATTRIBUTION_HEADER_NAME,
    ATTRIBUTION_SCHEMA_VERSION,
    GetAttributionContextRequest,
    GetAttributionContextResultFailure,
    GetAttributionContextResultSuccess,
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

# The complete set of keys allowed on the wire. A new field must consciously update these,
# which is the point: `project` is the only dimension Griptape Cloud matches budgets against,
# so it is the only one the engine sends, and `BaseNode.name` must never appear under any key.
ALLOWED_ENVELOPE_KEYS = {"v", "tags"}
ALLOWED_TAG_KEYS = {"project"}


def _decode(result: GetAttributionContextResultSuccess) -> dict[str, Any]:
    """Decode a Success payload's header value back into the dict the Cloud will read."""
    return json.loads(base64.urlsafe_b64decode(result.header_value))


def _tags(result: GetAttributionContextResultSuccess) -> dict[str, Any]:
    """The `tags` object the Cloud reads the project chain out of, or {} when none was sent."""
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
    """A stand-in engine whose project manager answers with a JSON-encodable value.

    A bare MagicMock hands back MagicMocks, which `json.dumps` cannot encode -- so the one
    peer the resolver reads is given a real value here and overridden per test.
    """
    mock_engine = MagicMock()
    mock_engine.project_manager.get_project_chain.return_value = []
    return mock_engine


def _entry(project_id: str, name: str | None) -> ProjectChainEntry:
    """A chain entry whose id and name never match, so an assertion cannot pass on the wrong one.

    The real type rather than a Mock: `name` is reserved on `Mock`, so `Mock(name="x").name`
    is a Mock rather than a string, and a mocked chain would answer with something that is
    neither a name nor `None`.
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


def _assert_warns_not_errors(manager: BudgetManager) -> None:
    """Assert a dispatch fails, and that its details are logged at WARNING rather than ERROR."""
    result = _dispatch(manager)

    assert isinstance(result, GetAttributionContextResultFailure)
    details = result.result_details
    assert isinstance(details, ResultDetails)
    assert [detail.level for detail in details.result_details] == [logging.WARNING]


class TestAttributionPayloadShape:
    """The decoded header is the contract; these pin its shape."""

    @pytest.fixture
    def budget_manager(self) -> BudgetManager:
        return BudgetManager(MagicMock(), engine=_mock_engine())

    def test_request_dispatches_through_the_engine(self, engine: Engine) -> None:
        """The request reaches BudgetManager over the real bus and comes back usable."""
        result = engine.handle_request(GetAttributionContextRequest())

        assert isinstance(result, GetAttributionContextResultSuccess)
        assert result.header_value
        assert result.header_name == ATTRIBUTION_HEADER_NAME
        assert result.schema_version == ATTRIBUTION_SCHEMA_VERSION

    def test_header_value_decodes_to_documented_payload(self, engine: Engine) -> None:
        """base64url of compact UTF-8 JSON, leaf-first chain, schema version 1."""
        result = engine.handle_request(GetAttributionContextRequest())
        assert isinstance(result, GetAttributionContextResultSuccess)

        assert _decode(result)["v"] == ATTRIBUTION_SCHEMA_VERSION
        # No project is selected on a bare engine, so the key is absent rather than empty.
        assert _tags(result).get("project", []) == result.project_chain

    def test_padding_round_trips(self, engine: Engine) -> None:
        """Padding is kept so the Cloud can decode without re-padding."""
        result = engine.handle_request(GetAttributionContextRequest())
        assert isinstance(result, GetAttributionContextResultSuccess)

        # Would raise binascii.Error on a stripped-padding value.
        base64.urlsafe_b64decode(result.header_value)
        assert len(result.header_value) % 4 == 0

    def test_payload_key_allowlist(self, engine: Engine, budget_manager: BudgetManager) -> None:
        """No key ships that has not been consciously added, chain or no chain.

        Both shapes are checked because they exercise different halves of the builder: the
        real engine has no project open and emits a bare envelope, while the mocked one emits
        the `tags` object that a new dimension would be smuggled into.
        """
        cast("MagicMock", budget_manager.engine).project_manager.get_project_chain.return_value = [
            _entry("leaf", "Leaf")
        ]

        for result in (engine.handle_request(GetAttributionContextRequest()), _succeed(budget_manager)):
            assert isinstance(result, GetAttributionContextResultSuccess)
            assert set(_decode(result)) <= ALLOWED_ENVELOPE_KEYS
            assert set(_tags(result)) <= ALLOWED_TAG_KEYS

        assert _tags(_succeed(budget_manager)) == {"project": ["leaf"]}

    def test_structured_fields_agree_with_the_header(self, budget_manager: BudgetManager) -> None:
        """A consumer reading the result must see exactly what the header encodes."""
        cast("MagicMock", budget_manager.engine).project_manager.get_project_chain.return_value = [
            _entry("leaf", "Leaf"),
            _entry("root", "Root"),
        ]

        result = _succeed(budget_manager)

        assert _tags(result)["project"] == result.project_chain

    def test_a_chainless_envelope_is_still_sent(self, budget_manager: BudgetManager) -> None:
        """`{"v": 1}` says "no project open"; sending nothing says "client does not attribute".

        The Cloud parser distinguishes the two, so the bare envelope carries a real fact and
        an empty chain is never a reason to withhold the header.
        """
        result = _succeed(budget_manager)

        assert _decode(result) == {"v": ATTRIBUTION_SCHEMA_VERSION}
        assert result.header_value


class TestProjectChain:
    """`tags.project` carries project ids, ordered leaf-first.

    The id is the wire value because it is what identifies a project across a rename -- the
    name would silently re-point that project's spend the moment a user edited it.
    """

    def _manager_on(self, project_manager: ProjectManager) -> BudgetManager:
        mock_engine = _mock_engine()
        mock_engine.project_manager = project_manager
        return BudgetManager(MagicMock(), engine=mock_engine)

    @pytest.mark.parametrize(
        "padded",
        ["  <system-defaults>  ", "\t<system-defaults>", "<system-defaults>\n", " <system-defaults>"],
    )
    def test_a_padded_sentinel_copy_never_reaches_the_wire(self, padded: str) -> None:
        """The far end strips before testing the reserved value, so a padded copy is the same claim.

        Left whole it is dropped there rather than here, and a dropped entry promotes its parent
        to leaf while `ENTRY_DROPPED` stays out of `mangled` -- so the short chain still matches
        a budget and bills a real ancestor. The exact string cannot get this far (it is the
        registry key for the rest state), but a padded one loads fine.
        """
        mock_engine = _mock_engine()
        mock_engine.project_manager.get_project_chain.return_value = [
            _entry(padded, "leaf"),
            _entry("acme-studios-0b12d8", "root"),
        ]
        result = _succeed(BudgetManager(MagicMock(), engine=mock_engine))

        assert _tags(result)["project"] == ["acme-studios-0b12d8"]
        assert result.project_chain == ["acme-studios-0b12d8"]

    def test_system_defaults_never_reaches_the_wire(self) -> None:
        """The Cloud reserves `<system-defaults>` and counts a client copy as degraded."""
        project_manager = ProjectManager(Mock(), Mock(), Mock())
        result = _succeed(self._manager_on(project_manager))

        assert "project" not in _tags(result)
        assert result.project_chain == []
        assert SYSTEM_DEFAULTS_KEY not in json.dumps(_decode(result))

    def test_the_default_templates_name_never_stands_in_for_the_sentinel(self) -> None:
        """Skipping `<system-defaults>` is keyed on its id, and its name is not sent either.

        The rest state is registered with the shipped `DEFAULT_PROJECT_TEMPLATE`, whose name
        is "Default Project" -- a generic string that would collide across every user in an
        org and match any budget rule unlucky enough to be written against it.
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

        assert _tags(result)["project"] == ["leaf"]

    def test_nested_chain_carries_every_id_leaf_first(self) -> None:
        """The whole ancestry travels, and nothing but ids does."""
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

    def test_a_chain_deeper_than_the_cloud_keeps_still_travels_whole(self) -> None:
        """Depth is the Cloud's to judge, not ours -- and judging it here is what hides it.

        Forty entries is past the parser's MAX_CHAIN_LENGTH of 32, so the Cloud truncates,
        records CHAIN_TRUNCATED, and marks the chain mangled, which takes it out of budget
        matching entirely. Cutting to 32 before sending would suppress all three: the chain
        arrives looking intact and matches a budget written against the wrong root.
        """
        project_manager = ProjectManager(Mock(), Mock(), Mock())
        ids = [f"p{index:02d}" for index in range(40)]
        # ids[0] is the leaf; each entry's parent is the next one along.
        for index, project_id in enumerate(ids):
            parent = ids[index + 1] if index + 1 < len(ids) else None
            _register_project(project_manager, project_id, name=f"Name {project_id}", parent_id=parent)
        project_manager._current_project_id = ids[0]

        result = _succeed(self._manager_on(project_manager))

        assert _tags(result)["project"] == ids
        assert result.project_chain == ids

    def test_a_chain_of_path_shaped_ids_travels_whole(self) -> None:
        """The realistic worst case for size, and it is not close to a problem.

        A project created before ids existed has the canonical path to its file written in as
        its id, so a deep chain of legacy projects is the longest header the engine can
        plausibly produce. Thirty-two of them encode to roughly 4 KB -- inside the Cloud's own
        raw-header cap and half of a default nginx header buffer -- so there is nothing here
        worth shortening, and shortening it would arrive looking intact anyway.
        """
        project_manager = ProjectManager(Mock(), Mock(), Mock())
        ids = [f"/Users/alice/Documents/projects/acme/season-02/shot-{index:03d}/project.yml" for index in range(32)]
        for index, project_id in enumerate(ids):
            parent = ids[index + 1] if index + 1 < len(ids) else None
            _register_project(project_manager, project_id, name=f"Shot {index}", parent_id=parent)
        project_manager._current_project_id = ids[0]

        result = _succeed(self._manager_on(project_manager))

        assert _tags(result)["project"] == ids
        assert len(result.header_value) < 5632  # noqa: PLR2004 -- the Cloud's MAX_RAW_HEADER_LENGTH

    def test_an_unregistered_parent_still_contributes_its_id(self) -> None:
        """The walk ends at an unregistered ancestor, but its id is a fact and travels.

        `get_project_chain` surfaces the id it could not resolve a template for and stops
        there. That id is what the parent is called everywhere else in the engine, so sending
        it is honest; deciding it is unmatchable and dropping the chain over it would be the
        engine judging on the Cloud's behalf.
        """
        project_manager = ProjectManager(Mock(), Mock(), Mock())
        _register_project(project_manager, "shot-6", name="Shot 6", parent_id="swx")
        project_manager._current_project_id = "shot-6"

        result = _succeed(self._manager_on(project_manager))

        assert _tags(result)["project"] == ["shot-6", "swx"]
        assert "Shot 6" not in result.header_value

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
        """`[]` would assert 'belongs to zero projects', which is a claim we cannot make.

        The envelope still goes out: no project open is a fact. An unreadable chain is not,
        and takes the Failure path instead -- see `TestDegradation`.
        """
        manager = BudgetManager(MagicMock(), engine=_mock_engine())
        result = _succeed(manager)

        assert "project" not in _tags(result)
        assert result.project_chain == []


class TestValuesTravelVerbatim:
    """Nothing here repairs an id. The Cloud reports what it had to repair; the engine does not.

    An id has no enforced shape -- no pattern, no length cap -- so it can carry anything a user
    typed or a filesystem produced. Every shape below is one the Cloud would strip, cut, or
    refuse to store, and every one of them still goes out exactly as the project stores it:
    a value repaired here arrives looking intact, which is the one thing the far end cannot
    detect and the one outcome worse than no attribution.
    """

    def _manager_with(self, *ids: str) -> BudgetManager:
        mock_engine = _mock_engine()
        mock_engine.project_manager.get_project_chain.return_value = [
            _entry(project_id, f"Name {index}") for index, project_id in enumerate(ids)
        ]
        return BudgetManager(MagicMock(), engine=mock_engine)

    def test_a_control_character_travels_unfiltered(self) -> None:
        """The Cloud drops an entry it cannot store, promotes its parent to leaf, and reports it.

        Withholding the chain here would trade that reported drop for a silent absence, and
        the engine cannot tell an id a user meant to type from one they pasted a newline into.
        """
        result = _succeed(self._manager_with("acme\nstudios", "root"))

        assert _tags(result)["project"] == ["acme\nstudios", "root"]

    def test_a_padded_id_travels_unstripped(self) -> None:
        """An id is an identifier, so it goes out as the project spells it.

        Stripping would be cheap and invisible, which is exactly the problem: `project_chain`
        would then report a string the engine cannot look the project up by.
        """
        result = _succeed(self._manager_with("  season-02-a3f9c1  "))

        assert _tags(result)["project"] == ["  season-02-a3f9c1  "]
        assert result.project_chain == ["  season-02-a3f9c1  "]

    def test_an_id_past_the_clouds_value_cap_travels_uncut(self) -> None:
        """Cutting is the far end's job, and doing it here would hide that it happened.

        The Cloud truncates a value past its 256-character cap, marks the chain mangled, and
        stops matching it against admin-authored paths. Pre-cutting hands it a prefix that
        looks intact, so it matches -- and two sibling projects sharing their first 256
        characters silently collapse onto one budget with nothing recorded.
        """
        long_id = "s" * 300
        result = _succeed(self._manager_with(long_id, "root"))

        assert _tags(result)["project"] == [long_id, "root"]
        assert result.project_chain == [long_id, "root"]

    def test_an_unencodable_id_costs_the_whole_header(self, caplog: pytest.LogCaptureFixture) -> None:
        """The one thing the wire physically cannot carry, and the module's only failure.

        A project created before ids existed carries the canonical path to its file as its id,
        so a path whose bytes are not valid UTF-8 arrives holding lone surrogates from
        `surrogateescape` -- and `str.encode` refuses them. There is no honest partial form to
        send instead: dropping just that entry would promote its parent to leaf and bill a
        real ancestor for spend it never incurred. So the header is given up and the call goes
        out unattributed.
        """
        manager = self._manager_with("renders-\udce9", "root")

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            result = _dispatch(manager)

        assert isinstance(result, GetAttributionContextResultFailure)
        assert "attribution" in str(result.result_details).lower()
        assert any(record.levelno == logging.WARNING for record in caplog.records)

    def test_the_encoder_returns_none_instead_of_raising(self) -> None:
        """Pinned on the module function, because the cost of raising is paid per metered call.

        A handler exception becomes a `GenericResultFailure`, which ignores `failure_log_level`;
        the explicit Failure returned instead can be quieted by a caller making many of these.
        """
        payload = {"v": 1, "tags": {"project": ["\udce9"]}}

        with pytest.raises(UnicodeEncodeError):
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

        assert budget_manager_module._encode_attribution_payload(payload) is None


class TestDegradation:
    """The caller is about to spend money: never raise, and never guess on its behalf."""

    def test_peer_failure_sends_no_header_rather_than_claiming_no_project(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An unreadable chain is not the same fact as an empty one, and must not borrow it.

        `{"v": 1}` is a positive claim -- the Cloud reads a missing `tags` as "no project open
        on a client that attributes". A project manager that raised knows nothing about whether
        a project is open, so sending that envelope would attribute an artist's spend to the
        default budget while recording no degradation on either side. No header at all reads as
        "this client did not attribute", which is true.
        """
        mock_engine = _mock_engine()
        mock_engine.project_manager.get_project_chain.side_effect = RuntimeError("peer exploded")
        manager = BudgetManager(MagicMock(), engine=mock_engine)

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            result = _dispatch(manager)

        assert isinstance(result, GetAttributionContextResultFailure)
        assert "attribution" in str(result.result_details).lower()
        assert any(record.levelno == logging.WARNING for record in caplog.records)

    def test_an_unreadable_chain_does_not_log_at_error(self) -> None:
        """A bare `result_details` string defaults to ERROR; both sites pass ResultDetails instead.

        Neither failure condition clears on its own -- a legacy project keeps its unencodable id,
        an unhappy peer stays unhappy -- so an ERROR would repeat once per metered call for
        something the artist cannot act on and that did not stop the work. Pinned on both paths
        because the ERROR default is silent.
        """
        mock_engine = _mock_engine()
        mock_engine.project_manager.get_project_chain.side_effect = RuntimeError("peer exploded")

        _assert_warns_not_errors(BudgetManager(MagicMock(), engine=mock_engine))

    def test_an_unencodable_id_does_not_log_at_error(self) -> None:
        """The other failure path, same reason."""
        mock_engine = _mock_engine()
        mock_engine.project_manager.get_project_chain.return_value = [_entry("renders-\udce9", "leaf")]

        _assert_warns_not_errors(BudgetManager(MagicMock(), engine=mock_engine))


class TestConfidentiality:
    """The header value is descriptive, not secret -- but it still stays out of the logs."""

    def test_request_does_not_broadcast(self) -> None:
        """`broadcast_result=False` keeps the Success payload off the WebSocket feed."""
        assert GetAttributionContextRequest().broadcast_result is False

    def test_broadcast_result_stays_keyword_only(self) -> None:
        """Pins `field(..., kw_only=True)` against a future bare redeclaration.

        The base payload is `kw_only=True` but this subclass is a plain `@dataclass`, so a
        bare `broadcast_result = False` would re-register it as the first positional
        parameter -- and broadcasting the header on every metered call is not something a
        stray positional argument should be able to turn on.
        """
        with pytest.raises(TypeError):
            GetAttributionContextRequest(True)  # type: ignore[misc]

    def test_header_value_is_never_logged(self, engine: Engine, caplog: pytest.LogCaptureFixture) -> None:
        """Neither the log stream nor `result_details`, which is logged as well as broadcast."""
        with caplog.at_level(logging.DEBUG, logger="griptape_nodes"):
            result = engine.handle_request(GetAttributionContextRequest())
        assert isinstance(result, GetAttributionContextResultSuccess)

        assert result.header_value not in caplog.text
        assert result.header_value not in str(result.result_details)

    def test_no_user_authored_label_appears(self, engine: Engine) -> None:
        """`BaseNode.name` and `ProjectTemplate.name` are user-authored and never travel.

        Ids are the only strings the payload carries now, so no key named `name` at any depth
        -- and the guard stays even though the builder makes it near-tautological, because it
        is what a future dimension would trip over.
        """
        result = engine.handle_request(GetAttributionContextRequest())
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

    def test_a_project_name_never_reaches_the_payload(self) -> None:
        """Every chain entry carries a name; none of them is ever sent."""
        mock_engine = _mock_engine()
        mock_engine.project_manager.get_project_chain.return_value = [
            _entry("leaf", "Acme Studios"),
            _entry("root", "Feature Film"),
        ]
        manager = BudgetManager(MagicMock(), engine=mock_engine)

        result = _succeed(manager)

        assert _tags(result)["project"] == ["leaf", "root"]
        for name in ("Acme Studios", "Feature Film"):
            assert name not in json.dumps(_decode(result))
            assert name not in result.header_value


class TestWiring:
    """The manager exists on every engine. Worker forwarding is covered in tests/unit/worker/."""

    def test_engine_exposes_the_budget_manager(self, engine: Engine) -> None:
        assert isinstance(engine.budget_manager, BudgetManager)
        assert engine.BudgetManager() is engine.budget_manager
