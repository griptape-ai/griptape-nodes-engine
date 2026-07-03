"""Strict-mode enforcement framework.

Strict mode is a runtime contract for what node code is allowed to do
across the orchestrator/worker split. This module owns scope, severity
resolution, and violation reporting -- and nothing else.

Two kinds of strict-mode scope exist:

* ``RUNTIME_EXECUTE`` opens around ``NodeManager.on_execute_node_request``
  for a single node's execution.
* ``LOAD_PROBE`` opens around each class's schema probe in
  ``LibraryManager._serialize_library_node_schemas``.

Detectors live at their own call sites (e.g. ``EventManager.handle_request``,
``BaseNode.add_parameter``) and import the module-level singleton
``STRICT_MODE`` to record violations:

    from griptape_nodes.common.strict_mode import STRICT_MODE
    STRICT_MODE.report(rule_id=..., message=...)

Severity is picked per-rule by the reporter's severity resolver:
correctness rules fail on both sides, ergonomics rules warn on the
orchestrator and escalate to ERROR on the worker. Callers that need to
escalate a worker violation (e.g. convert ``ExecuteNodeResultSuccess``
to a failure, or skip a class's schema) inspect the scope's
``violations`` list after exit.

"Am I currently constructing a node?" and "What request is currently
being dispatched?" are NOT owned here. Those facts belong to
``LibraryRegistry`` and ``EventManager`` respectively; detectors
consult those subsystems directly.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from griptape_nodes.retained_mode.events.base_events import ResultPayload

# Env-var override so operators can disable strict mode wholesale without a code
# change. Production parity is the default; the kill switch is an escape hatch
# for performance triage or for environments where the orchestrator/worker split
# is known to be intentionally violated (e.g. legacy single-process tests).
_STRICT_MODE_DISABLED_ENV = "GTN_STRICT_MODE_DISABLED"


class SeverityResolver(Protocol):
    """Callable signature for severity resolution.

    The default implementation looks the rule up in ``RULES`` and applies
    the correctness/escalation policy. Tests inject fakes (typically
    constant-returning) by passing ``severity_resolver=`` to
    :class:`StrictModeReporter`.
    """

    def __call__(self, *, rule_id: str, is_worker: bool) -> StrictModeSeverity: ...


class StrictModeScopeKind(StrEnum):
    RUNTIME_EXECUTE = "runtime_execute"
    LOAD_PROBE = "load_probe"


class StrictModeSeverity(StrEnum):
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class StrictModeViolation:
    rule_id: str
    severity: StrictModeSeverity
    scope_kind: StrictModeScopeKind
    subject: str
    library_name: str | None
    message: str


@dataclass
class StrictModeScope:
    kind: StrictModeScopeKind
    subject: str
    library_name: str | None
    is_worker: bool
    violations: list[StrictModeViolation] = field(default_factory=list)


@dataclass
class ScopedResult:
    """Mutable result slot yielded by ``StrictModeReporter.scoped_execution``.

    The ``async with`` body assigns ``result`` after the wrapped runner
    returns. The reporter's teardown reads this slot and merges the
    scope's violations into it. Callers that need to apply a domain-specific
    escalation policy (e.g. promote a worker success to a failure when
    correctness violations fired) can inspect the scope and reassign
    ``result`` from inside the body before the context exits.
    """

    result: ResultPayload | None = None


def _default_severity_resolver(*, rule_id: str, is_worker: bool) -> StrictModeSeverity:
    """Resolve the severity of a reported rule against the static registry.

    Correctness rules ({@code correctness=True} in the registry) fail on
    both sides. Non-correctness rules warn on the orchestrator and
    escalate to ERROR on the worker (subject to the rule's
    ``worker_escalation`` flag). Unregistered rules fall back to the
    historical worker=ERROR / orchestrator=WARNING split so detectors
    can land before the registry entry is added.
    """
    # Lazy import: strict_mode_checks imports StrictModeSeverity from this
    # module, so a top-level import would cycle once RULES is non-empty.
    from griptape_nodes.common.strict_mode_checks import RULES

    rule = RULES.get(rule_id)
    if rule is None:
        # A typo'd or unregistered rule_id should not silently produce a
        # violation. Log loudly so it surfaces in dev; production callers
        # still get the historical worker=ERROR / orchestrator=WARNING
        # fallback so existing detectors are not broken by the change.
        logging.getLogger("griptape_nodes.strict_mode").warning(
            "strict-mode rule '%s' is not registered in RULES. Falling back to worker=%s default severity.",
            rule_id,
            "ERROR" if is_worker else "WARNING",
        )
        return StrictModeSeverity.ERROR if is_worker else StrictModeSeverity.WARNING
    if rule.correctness:
        return StrictModeSeverity.ERROR
    if is_worker and rule.worker_escalation:
        return StrictModeSeverity.ERROR
    return StrictModeSeverity.WARNING


class StrictModeReporter:
    """Owns the scope stack and violation reporting.

    Detectors interact via the process-level singleton ``STRICT_MODE``
    declared at module bottom. The class is parameterized by a severity
    resolver and a logger so tests can inject fakes.

    Each instance owns its own ContextVar-backed scope stack. The
    framework does not expect more than one instance in production --
    instantiating a second reporter creates an independent stack and
    detectors will report against whichever singleton they imported.
    """

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        severity_resolver: SeverityResolver | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._enabled = enabled if enabled is not None else os.environ.get(_STRICT_MODE_DISABLED_ENV) != "1"
        self._severity_resolver: SeverityResolver = severity_resolver or _default_severity_resolver
        self._logger = logger or logging.getLogger("griptape_nodes.strict_mode")
        self._scope_stack: ContextVar[tuple[StrictModeScope, ...]] = ContextVar(
            f"_strict_mode_scope_stack_{id(self)}", default=()
        )

    @property
    def enabled(self) -> bool:
        """Whether the reporter is currently active.

        When false, ``open_scope`` yields a detached scope that is not on
        the per-task stack, so ``current_scope()`` returns None and
        ``report()`` no-ops. ``attach_violations_to_result`` is a no-op
        in practice only because the detached scope's ``violations`` list
        stays empty -- a caller that hands in a manually-built scope with
        violations will still mutate ``result``. Detectors do not need to
        branch on this; the reporter swallows the work itself.
        """
        return self._enabled

    @contextmanager
    def open_scope(
        self,
        *,
        kind: StrictModeScopeKind,
        subject: str,
        library_name: str | None,
        is_worker: bool,
    ) -> Iterator[StrictModeScope]:
        """Push a new strict-mode scope onto the per-task stack.

        Nested scopes restore the outer on exit. ``current_scope()`` and
        ``report()`` always operate on the innermost. When the reporter
        is disabled the yielded scope is detached (not on the stack), so
        ``report()`` finds no active scope and no-ops.
        """
        scope = StrictModeScope(kind=kind, subject=subject, library_name=library_name, is_worker=is_worker)
        if not self._enabled:
            yield scope
            return
        previous = self._scope_stack.get()
        token = self._scope_stack.set((*previous, scope))
        try:
            yield scope
        finally:
            self._scope_stack.reset(token)

    @asynccontextmanager
    async def scoped_execution(
        self,
        *,
        kind: StrictModeScopeKind,
        subject: str,
        library_name: str | None,
        is_worker: bool,
    ) -> AsyncIterator[tuple[ScopedResult, StrictModeScope]]:
        """Open a scope, yield a (result-slot, scope) pair, attach on exit.

        Async wrapper around :meth:`open_scope` that owns the
        ``attach_violations_to_result`` boilerplate so request handlers
        do not have to. Usage::

            async with STRICT_MODE.scoped_execution(...) as (ctx, scope):
                ctx.result = await runner()
                # optionally inspect scope.violations and reassign ctx.result
                # to apply a domain-specific escalation policy.

        On exit the helper raises if ``ctx.result`` was never assigned
        (programmer error), then merges ``scope.violations`` into the
        final payload via ``attach_violations_to_result``. The escalation
        policy itself stays with the caller because it is execute-context
        specific (e.g. only ``ExecuteNodeResultSuccess`` knows how to
        promote to a failure).
        """
        with self.open_scope(
            kind=kind,
            subject=subject,
            library_name=library_name,
            is_worker=is_worker,
        ) as scope:
            ctx = ScopedResult()
            yield ctx, scope
            if ctx.result is None:
                msg = (
                    f"scoped_execution body for subject '{subject}' did not assign ctx.result; "
                    "this is a programmer error in the caller."
                )
                raise RuntimeError(msg)
            self.attach_violations_to_result(ctx.result, scope)

    def current_scope(self) -> StrictModeScope | None:
        """Return the innermost active scope on the current task, or None."""
        stack = self._scope_stack.get()
        if not stack:
            return None
        return stack[-1]

    def report(self, *, rule_id: str, message: str) -> StrictModeScope | None:
        """Record a violation against the innermost active scope.

        No-op when no scope is active. Returns the scope (with the new
        violation appended to ``violations``) so callers can inspect or
        pass it along.
        """
        scope = self.current_scope()
        if scope is None:
            return None
        severity = self._severity_resolver(rule_id=rule_id, is_worker=scope.is_worker)
        violation = StrictModeViolation(
            rule_id=rule_id,
            severity=severity,
            scope_kind=scope.kind,
            subject=scope.subject,
            library_name=scope.library_name,
            message=message,
        )
        scope.violations.append(violation)
        subject_label = "node" if scope.kind is StrictModeScopeKind.RUNTIME_EXECUTE else "class"
        log = self._logger.error if severity is StrictModeSeverity.ERROR else self._logger.warning
        log(
            "strict-mode [%s/%s] %s=%s library=%s: %s",
            scope.kind.value,
            severity.value,
            subject_label,
            scope.subject,
            scope.library_name,
            message,
        )
        return scope

    def attach_violations_to_result(self, result: ResultPayload, scope: StrictModeScope) -> None:
        """Append ``scope.violations`` onto ``result.result_details`` in place.

        ``ResultPayload.__post_init__`` has already coerced any string
        ``result_details`` into a ``ResultDetails`` instance, so by the
        time this runs the union has been narrowed to ``ResultDetails``.
        Each violation becomes a ``StrictModeViolationDetail`` appended
        to the existing list. Mutates ``result``; returns ``None``.
        """
        # Lazy import: base_events declares the result/violation dataclasses
        # the framework consumes here, but base_events also imports STRICT_MODE
        # at its detector sites. Keeping the cycle break in this single
        # framework method lets every detector module import strict-mode
        # symbols at the top of the file.
        from griptape_nodes.retained_mode.events.base_events import ResultDetails, StrictModeViolationDetail

        if not scope.violations:
            return

        details = result.result_details
        if not isinstance(details, ResultDetails):
            msg = (
                f"attach_violations_to_result expected result_details to be ResultDetails "
                f"after __post_init__ coercion, got {type(details).__name__}."
            )
            raise TypeError(msg)
        for violation in scope.violations:
            level = logging.ERROR if violation.severity is StrictModeSeverity.ERROR else logging.WARNING
            details.result_details.append(
                StrictModeViolationDetail(
                    level=level,
                    message=violation.message,
                    rule_id=violation.rule_id,
                    severity=violation.severity.value,
                    subject=violation.subject,
                    library_name=violation.library_name,
                )
            )


STRICT_MODE = StrictModeReporter()
