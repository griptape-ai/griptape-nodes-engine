from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

from griptape_nodes.common.strict_mode import (
    STRICT_MODE,
    StrictModeReporter,
    StrictModeScopeKind,
    StrictModeSeverity,
)
from griptape_nodes.common.strict_mode_checks import RULES, StrictModeRule
from griptape_nodes.retained_mode.events.base_events import (
    ResultDetails,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    StrictModeViolationDetail,
)


@dataclass(kw_only=True)
class _FakeSuccessResult(ResultPayloadSuccess):
    pass


@dataclass(kw_only=True)
class _FakeFailureResult(ResultPayloadFailure):
    pass


@pytest.fixture
def fake_rules() -> Iterator[None]:
    """Register two synthetic rules for the duration of the test.

    The framework PR ships an empty RULES catalog; rule PRs append entries
    later in the stack. Tests at this layer need a registered rule_id to
    exercise severity resolution, so they install one and tear it down.
    """
    ergonomics = StrictModeRule(
        rule_id="fake-ergonomics",
        default_severity=StrictModeSeverity.WARNING,
        correctness=False,
        description="synthetic ergonomics rule for tests",
        remediation_template="ergonomics: {detail}",
        worker_escalation=False,
    )
    correctness = StrictModeRule(
        rule_id="fake-correctness",
        default_severity=StrictModeSeverity.ERROR,
        correctness=True,
        description="synthetic correctness rule for tests",
        remediation_template="correctness: {detail}",
    )
    RULES[ergonomics.rule_id] = ergonomics
    RULES[correctness.rule_id] = correctness
    try:
        yield
    finally:
        RULES.pop(ergonomics.rule_id, None)
        RULES.pop(correctness.rule_id, None)


@pytest.fixture
def reporter() -> StrictModeReporter:
    """A fresh reporter so each test owns its own scope stack.

    Tests that go through ``STRICT_MODE`` directly (the production
    singleton) would inherit any leaked scope state from earlier tests.
    Per-test reporters give each case a clean ContextVar.
    """
    return StrictModeReporter()


class TestStrictModeReporter:
    def test_default_singleton_exists(self) -> None:
        assert isinstance(STRICT_MODE, StrictModeReporter)

    def test_current_scope_is_none_outside_any_scope(self, reporter: StrictModeReporter) -> None:
        assert reporter.current_scope() is None

    def test_open_scope_sets_and_resets_contextvar(self, reporter: StrictModeReporter) -> None:
        with reporter.open_scope(
            kind=StrictModeScopeKind.RUNTIME_EXECUTE,
            subject="node-1",
            library_name="libA",
            is_worker=False,
        ) as scope:
            assert reporter.current_scope() is scope
            assert scope.kind is StrictModeScopeKind.RUNTIME_EXECUTE
            assert scope.subject == "node-1"
            assert scope.library_name == "libA"
            assert scope.is_worker is False
            assert scope.violations == []
        assert reporter.current_scope() is None

    def test_nested_scopes_restore_outer_on_exit(self, reporter: StrictModeReporter) -> None:
        with reporter.open_scope(
            kind=StrictModeScopeKind.RUNTIME_EXECUTE,
            subject="outer",
            library_name=None,
            is_worker=False,
        ) as outer:
            assert reporter.current_scope() is outer
            with reporter.open_scope(
                kind=StrictModeScopeKind.LOAD_PROBE,
                subject="inner",
                library_name="libX",
                is_worker=True,
            ) as inner:
                assert reporter.current_scope() is inner
            assert reporter.current_scope() is outer
        assert reporter.current_scope() is None

    def test_violations_attach_to_correct_scope_when_nested(
        self,
        reporter: StrictModeReporter,
        fake_rules: None,  # noqa: ARG002
    ) -> None:
        with reporter.open_scope(
            kind=StrictModeScopeKind.RUNTIME_EXECUTE,
            subject="outer",
            library_name=None,
            is_worker=False,
        ) as outer:
            reporter.report(rule_id="fake-ergonomics", message="outer-1")
            with reporter.open_scope(
                kind=StrictModeScopeKind.LOAD_PROBE,
                subject="inner",
                library_name=None,
                is_worker=True,
            ) as inner:
                reporter.report(rule_id="fake-correctness", message="inner-1")
            reporter.report(rule_id="fake-ergonomics", message="outer-2")

        assert [v.message for v in outer.violations] == ["outer-1", "outer-2"]
        assert [v.message for v in inner.violations] == ["inner-1"]


class TestReporterReport:
    def test_no_op_outside_scope(self, reporter: StrictModeReporter, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger="griptape_nodes.strict_mode")
        result = reporter.report(rule_id="r", message="m")
        assert result is None
        assert caplog.records == []

    def test_orchestrator_logs_warning(
        self,
        reporter: StrictModeReporter,
        caplog: pytest.LogCaptureFixture,
        fake_rules: None,  # noqa: ARG002
    ) -> None:
        rule_id = "fake-ergonomics"
        caplog.set_level(logging.DEBUG, logger="griptape_nodes.strict_mode")
        with reporter.open_scope(
            kind=StrictModeScopeKind.RUNTIME_EXECUTE,
            subject="n",
            library_name="libA",
            is_worker=False,
        ) as scope:
            reporter.report(rule_id=rule_id, message="something bad")
            assert len(scope.violations) == 1
            v = scope.violations[0]
            assert v.rule_id == rule_id
            assert v.severity is StrictModeSeverity.WARNING
            assert v.scope_kind is StrictModeScopeKind.RUNTIME_EXECUTE
            assert v.subject == "n"
            assert v.library_name == "libA"
            assert v.message == "something bad"

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(warnings) == 1
        assert len(errors) == 0
        assert "something bad" in warnings[0].getMessage()

    def test_worker_logs_error(
        self,
        reporter: StrictModeReporter,
        caplog: pytest.LogCaptureFixture,
        fake_rules: None,  # noqa: ARG002
    ) -> None:
        rule_id = "fake-correctness"
        caplog.set_level(logging.DEBUG, logger="griptape_nodes.strict_mode")
        with reporter.open_scope(
            kind=StrictModeScopeKind.RUNTIME_EXECUTE,
            subject="n",
            library_name="libA",
            is_worker=True,
        ) as scope:
            reporter.report(rule_id=rule_id, message="very bad")
            assert scope.violations[0].severity is StrictModeSeverity.ERROR

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(errors) == 1
        assert len(warnings) == 0


class TestSeverityResolverInjection:
    def test_custom_resolver_is_consulted(self, fake_rules: None) -> None:  # noqa: ARG002
        recorded: list[tuple[str, bool]] = []

        def fake_resolver(*, rule_id: str, is_worker: bool) -> StrictModeSeverity:
            recorded.append((rule_id, is_worker))
            return StrictModeSeverity.ERROR

        reporter = StrictModeReporter(severity_resolver=fake_resolver)
        with reporter.open_scope(
            kind=StrictModeScopeKind.RUNTIME_EXECUTE,
            subject="n",
            library_name=None,
            is_worker=False,
        ) as scope:
            reporter.report(rule_id="fake-ergonomics", message="x")

        assert recorded == [("fake-ergonomics", False)]
        assert scope.violations[0].severity is StrictModeSeverity.ERROR


class TestAttachViolationsToResult:
    def _scope_with(self, reporter: StrictModeReporter, *violations: tuple[str, str]) -> object:
        with reporter.open_scope(
            kind=StrictModeScopeKind.RUNTIME_EXECUTE,
            subject="n",
            library_name="libA",
            is_worker=False,
        ) as scope:
            for rule_id, message in violations:
                reporter.report(rule_id=rule_id, message=message)
        return scope

    def test_no_violations_leaves_details_unchanged(self, reporter: StrictModeReporter) -> None:
        result = _FakeSuccessResult(result_details="ok")
        original_details = result.result_details
        scope_obj = self._scope_with(reporter)  # zero violations
        reporter.attach_violations_to_result(result, scope_obj)  # type: ignore[arg-type]
        assert result.result_details is original_details
        assert isinstance(result.result_details, ResultDetails)
        assert len(result.result_details.result_details) == 1  # the original "ok" detail only

    def test_appends_violation_detail_to_success_result(self, reporter: StrictModeReporter, fake_rules: None) -> None:  # noqa: ARG002
        result = _FakeSuccessResult(result_details="ok")
        scope_obj = self._scope_with(reporter, ("fake-ergonomics", "naughty"))
        reporter.attach_violations_to_result(result, scope_obj)  # type: ignore[arg-type]

        expected_count = 2  # original "ok" + the violation
        assert isinstance(result.result_details, ResultDetails)
        details = result.result_details.result_details
        assert len(details) == expected_count
        violation_detail = details[-1]
        assert isinstance(violation_detail, StrictModeViolationDetail)
        assert violation_detail.rule_id == "fake-ergonomics"
        assert violation_detail.severity == StrictModeSeverity.WARNING.value
        assert violation_detail.subject == "n"
        assert violation_detail.library_name == "libA"
        assert violation_detail.level == logging.WARNING

    def test_failure_with_string_detail_keeps_error_level(self, reporter: StrictModeReporter, fake_rules: None) -> None:  # noqa: ARG002
        # Regression: previously attach_violations_to_result wrapped a string
        # result_details as level=DEBUG, silently demoting failure messages.
        # ResultPayloadFailure.__post_init__ now coerces to level=ERROR; the
        # attach routine must not touch the existing detail.
        result = _FakeFailureResult(result_details="boom")
        scope_obj = self._scope_with(reporter, ("fake-correctness", "very bad"))
        reporter.attach_violations_to_result(result, scope_obj)  # type: ignore[arg-type]

        assert isinstance(result.result_details, ResultDetails)
        details = result.result_details.result_details
        assert details[0].level == logging.ERROR
        assert details[0].message == "boom"
        violation_detail = details[1]
        assert isinstance(violation_detail, StrictModeViolationDetail)
        assert violation_detail.level == logging.ERROR

    def test_returns_none(self, reporter: StrictModeReporter, fake_rules: None) -> None:  # noqa: ARG002
        result = _FakeSuccessResult(result_details="ok")
        scope_obj = self._scope_with(reporter, ("fake-ergonomics", "x"))
        returned = reporter.attach_violations_to_result(result, scope_obj)  # type: ignore[arg-type]
        assert returned is None


class TestEnabledToggle:
    def test_disabled_reporter_skips_open_scope_stack(self) -> None:
        reporter = StrictModeReporter(enabled=False)
        with reporter.open_scope(
            kind=StrictModeScopeKind.RUNTIME_EXECUTE,
            subject="n",
            library_name=None,
            is_worker=False,
        ) as scope:
            # Detached scope: report() finds no active scope and no-ops.
            assert reporter.current_scope() is None
            assert reporter.report(rule_id="r", message="m") is None
            assert scope.violations == []

    def test_enabled_reporter_pushes_onto_stack(self) -> None:
        reporter = StrictModeReporter(enabled=True)
        with reporter.open_scope(
            kind=StrictModeScopeKind.RUNTIME_EXECUTE,
            subject="n",
            library_name=None,
            is_worker=False,
        ) as scope:
            assert reporter.current_scope() is scope

    def test_env_var_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GTN_STRICT_MODE_DISABLED", "1")
        reporter = StrictModeReporter()
        assert reporter.enabled is False

    def test_env_var_unset_enables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GTN_STRICT_MODE_DISABLED", raising=False)
        reporter = StrictModeReporter()
        assert reporter.enabled is True

    def test_explicit_enabled_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GTN_STRICT_MODE_DISABLED", "1")
        reporter = StrictModeReporter(enabled=True)
        assert reporter.enabled is True


class TestParallelTaskIsolation:
    @pytest.mark.asyncio
    async def test_concurrent_tasks_have_independent_scopes(self, reporter: StrictModeReporter) -> None:
        seen: dict[str, tuple[str, int]] = {}

        async def run_one(name: str, *, is_worker: bool) -> None:
            with reporter.open_scope(
                kind=StrictModeScopeKind.RUNTIME_EXECUTE,
                subject=name,
                library_name=None,
                is_worker=is_worker,
            ) as scope:
                reporter.report(rule_id="r", message=f"msg-{name}")
                await asyncio.sleep(0)
                reporter.report(rule_id="r", message=f"msg-{name}-2")
                seen[name] = (scope.subject, len(scope.violations))

        await asyncio.gather(
            run_one("a", is_worker=False),
            run_one("b", is_worker=True),
            run_one("c", is_worker=False),
        )

        assert seen == {"a": ("a", 2), "b": ("b", 2), "c": ("c", 2)}
