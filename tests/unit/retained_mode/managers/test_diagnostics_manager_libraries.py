"""Tests for what a report says about a library's worker, and why it cannot run.

A library whose nodes execute in a separate process has two ways of being broken, and they
send the reader in opposite directions. The worker never started, or the worker started and
the library inside it did not. `worker_ready` is the field that tells those apart, so a wrong
answer here costs whoever reads the bundle the one thing it was collected for.

The wrong answer is specifically a false yes. Readiness is a gate the worker manager installs
when a spawn is requested, and it reports an absent gate as settled -- a deliberate choice
there, since code waiting on a library that will never spawn should not wait forever. Read as
"settled means up", it says every library whose worker was never asked for has one running.
That is not an edge case: workers start with a session, and a bundle collected from the CLI is
collected before any session begins, so it would be every worker library in the report.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock

from griptape_nodes.common.diagnostics.redaction import Redactor
from griptape_nodes.retained_mode.managers.diagnostics_manager import DiagnosticsManager
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager

if TYPE_CHECKING:
    from griptape_nodes.common.diagnostics.report import LibraryDiagnostics

_LIBRARY_PATH = "/libraries/painter/griptape_nodes_library.json"


def _lib_info(
    *,
    library_name: str | None = "Painter",
    requires_worker: bool = False,
    executes_in_worker: bool = False,
    execution_unavailable_reason: str | None = None,
    execution_env_failure: str | None = None,
) -> LibraryManager.LibraryInfo:
    """A loaded, healthy library, with the worker fields left to the caller."""
    return LibraryManager.LibraryInfo(
        lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
        fitness=LibraryManager.LibraryFitness.GOOD,
        library_path=_LIBRARY_PATH,
        is_sandbox=False,
        library_name=library_name,
        requires_worker=requires_worker,
        executes_in_worker=executes_in_worker,
        execution_unavailable_reason=execution_unavailable_reason,
        execution_env_failure=execution_env_failure,
    )


def _entry(
    lib_info: LibraryManager.LibraryInfo,
    *,
    worker_registered: bool = False,
    worker_unavailable_reason: str | None = None,
    secret_values: list[str] | None = None,
) -> LibraryDiagnostics:
    """The one entry a report holds for `lib_info`, against a worker manager saying the above.

    Both worker answers are given explicitly because a bare `Mock` answers both truthily,
    which would read as a library that cannot run and has a worker running it at once.
    """
    engine = Mock()
    engine.library_manager.get_libraries_attempted_to_load.return_value = [_LIBRARY_PATH]
    engine.library_manager.get_library_info_for_attempted_load.return_value = lib_info
    engine.library_manager.collate_problems_for_lib_info.return_value = None
    engine.worker_manager.worker_unavailable_reason.return_value = worker_unavailable_reason
    if worker_registered:
        engine.worker_manager.get_worker_for_key.return_value = ("worker-1", "worker-1-requests")
    else:
        engine.worker_manager.get_worker_for_key.return_value = None

    manager = DiagnosticsManager(Mock(), engine=engine)
    redactor = Redactor(secret_values=secret_values or [], normalize_identity=False)
    entries = manager._build_libraries_section(redactor, [])

    assert len(entries) == 1
    return entries[0]


class TestWorkerReady:
    def test_a_library_that_runs_in_this_process_is_not_asked_about_a_worker(self) -> None:
        """Set to a bool either way, the field would read as a worker problem on every library."""
        entry = _entry(_lib_info(executes_in_worker=False))

        assert entry.worker_ready is None
        assert entry.worker_unavailable_reason is None

    def test_a_worker_that_was_never_asked_for_is_not_reported_as_up(self) -> None:
        """The regression: the whole libraries section claiming workers that do not exist.

        Nothing is recorded against this library -- no refusal, no gate, no registration --
        which is exactly how a worker library looks in an engine that never opened a session.
        """
        entry = _entry(_lib_info(executes_in_worker=True))

        assert entry.worker_ready is False

    def test_a_registered_worker_is_reported_as_up(self) -> None:
        entry = _entry(_lib_info(executes_in_worker=True), worker_registered=True)

        assert entry.worker_ready is True

    def test_a_library_with_no_name_yet_is_not_asked_about_a_worker(self) -> None:
        """The worker manager keys on the name, so there is nothing to ask for."""
        entry = _entry(_lib_info(library_name=None, executes_in_worker=True), worker_registered=True)

        assert entry.worker_ready is None
        assert entry.worker_unavailable_reason is None


class TestWhyNoWorkerIsServingIt:
    """`worker_ready: false` on its own is a dead end, so the report carries the reason."""

    def test_the_reason_on_the_library_is_used_first(self) -> None:
        """It applies wherever the nodes run, and it is what the user's error already said."""
        entry = _entry(
            _lib_info(executes_in_worker=True, execution_unavailable_reason="this machine has no GPU."),
            worker_unavailable_reason="the worker did not report in.",
        )

        assert entry.worker_unavailable_reason == "this machine has no GPU."

    def test_the_worker_managers_reason_is_used_when_the_library_has_none(self) -> None:
        entry = _entry(
            _lib_info(executes_in_worker=True),
            worker_unavailable_reason="the worker process exited at startup.",
        )

        assert entry.worker_unavailable_reason == "the worker process exited at startup."

    def test_a_failed_execution_environment_build_is_reported_before_any_spawn(self) -> None:
        """The spawn path hands this reason over, and a build that failed is why it never ran."""
        entry = _entry(_lib_info(executes_in_worker=True, execution_env_failure="uv could not resolve torch==99.0."))

        assert entry.worker_unavailable_reason == "uv could not resolve torch==99.0."

    def test_a_secret_in_the_reason_is_hidden(self) -> None:
        """These strings carry uv output, which carries index URLs with credentials in them."""
        entry = _entry(
            _lib_info(executes_in_worker=True, execution_env_failure="uv failed against https://tok-abc123@index/"),
            secret_values=["tok-abc123"],
        )

        assert "tok-abc123" not in (entry.worker_unavailable_reason or "")


class TestWorkerDeclarations:
    """Both flags are reported, because they are not the same question.

    Legacy worker mode keeps a library's nodes from loading in this process at all, so its
    node classes arrive as stubs from the worker. Execution dependencies load the real nodes
    here and send only `process()` out. A report that collapsed the two would leave a reader
    unable to tell an empty sidebar entry from a node that cannot run.
    """

    def test_a_library_running_only_its_execution_in_a_worker(self) -> None:
        entry = _entry(_lib_info(requires_worker=False, executes_in_worker=True))

        assert entry.requires_worker is False
        assert entry.executes_in_worker is True

    def test_a_legacy_worker_mode_library(self) -> None:
        entry = _entry(_lib_info(requires_worker=True, executes_in_worker=True))

        assert entry.requires_worker is True
        assert entry.executes_in_worker is True

    def test_a_library_that_needs_no_worker_at_all(self) -> None:
        entry = _entry(_lib_info())

        assert entry.requires_worker is False
        assert entry.executes_in_worker is False
