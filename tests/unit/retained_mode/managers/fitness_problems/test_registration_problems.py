"""Tests for the library registration and worker-compatibility fitness problem classes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.managers.fitness_problems.libraries import (
    AppEventListenerRegistrationProblem,
    PostDispatchHookRegistrationProblem,
    PostDispatchHooksWorkerIncompatibleProblem,
    PreDispatchHookRegistrationProblem,
    RequestHandlerRegistrationProblem,
)

if TYPE_CHECKING:
    import pytest


class TestAppEventListenerRegistrationProblem:
    def test_collate_single_problem(self) -> None:
        problem = AppEventListenerRegistrationProblem(error_message="listener boom")
        result = AppEventListenerRegistrationProblem.collate_problems_for_display([problem])
        assert "listener boom" in result

    def test_collate_multiple_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        problems = [
            AppEventListenerRegistrationProblem(error_message="err1"),
            AppEventListenerRegistrationProblem(error_message="err2"),
        ]
        with caplog.at_level("ERROR"):
            AppEventListenerRegistrationProblem.collate_problems_for_display(problems)
        assert caplog.records  # warning/error was logged


class TestPostDispatchHookRegistrationProblem:
    def test_collate_single_problem(self) -> None:
        problem = PostDispatchHookRegistrationProblem(error_message="hook boom")
        result = PostDispatchHookRegistrationProblem.collate_problems_for_display([problem])
        assert "hook boom" in result

    def test_collate_multiple_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        problems = [
            PostDispatchHookRegistrationProblem(error_message="err1"),
            PostDispatchHookRegistrationProblem(error_message="err2"),
        ]
        with caplog.at_level("ERROR"):
            result = PostDispatchHookRegistrationProblem.collate_problems_for_display(problems)
        assert caplog.records
        assert "err1" in result
        assert "err2" in result


class TestPostDispatchHooksWorkerIncompatibleProblem:
    """The collated string is the only explanation an artist gets for a silent hook."""

    def test_collate_names_the_library_and_the_hook_count(self) -> None:
        problem = PostDispatchHooksWorkerIncompatibleProblem(library_name="My Library", hook_count=3)
        result = PostDispatchHooksWorkerIncompatibleProblem.collate_problems_for_display([problem])
        assert "My Library" in result
        assert "3" in result

    def test_collate_multiple_logs_error(self, caplog: pytest.LogCaptureFixture) -> None:
        problems = [
            PostDispatchHooksWorkerIncompatibleProblem(library_name="LibA", hook_count=1),
            PostDispatchHooksWorkerIncompatibleProblem(library_name="LibB", hook_count=2),
        ]
        with caplog.at_level("ERROR"):
            PostDispatchHooksWorkerIncompatibleProblem.collate_problems_for_display(problems)
        assert caplog.records


class TestPreDispatchHookRegistrationProblem:
    def test_collate_single_problem(self) -> None:
        problem = PreDispatchHookRegistrationProblem(error_message="hook boom")
        result = PreDispatchHookRegistrationProblem.collate_problems_for_display([problem])
        assert "hook boom" in result

    def test_collate_multiple_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        problems = [
            PreDispatchHookRegistrationProblem(error_message="err1"),
            PreDispatchHookRegistrationProblem(error_message="err2"),
        ]
        with caplog.at_level("ERROR"):
            result = PreDispatchHookRegistrationProblem.collate_problems_for_display(problems)
        assert caplog.records
        assert "err1" in result
        assert "err2" in result


class TestRequestHandlerRegistrationProblem:
    def test_collate_single_problem(self) -> None:
        problem = RequestHandlerRegistrationProblem(error_message="handler boom")
        result = RequestHandlerRegistrationProblem.collate_problems_for_display([problem])
        assert "handler boom" in result

    def test_collate_multiple_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        problems = [
            RequestHandlerRegistrationProblem(error_message="err1"),
            RequestHandlerRegistrationProblem(error_message="err2"),
        ]
        with caplog.at_level("ERROR"):
            RequestHandlerRegistrationProblem.collate_problems_for_display(problems)
        assert caplog.records
