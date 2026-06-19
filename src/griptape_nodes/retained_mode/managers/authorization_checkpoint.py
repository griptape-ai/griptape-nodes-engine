"""Authorization checkpoints: extension points the engine offers for gating.

The engine cannot evaluate permissions itself -- it knows nothing about the
license, Cedar, or the app that wraps it. Instead, at the points where a
privileged operation is about to happen (loading a library past its metadata
stage, instantiating a node, activating a project, invoking a model) it
constructs an `AuthorizationCheckpoint` describing the resolved subject and asks
any registered hook whether to proceed. The app registers such a hook (alongside
the pre-dispatch hook it already installs on `EventManager`) and answers with a
`CheckpointDenial` or `None`.

This keeps the dependency arrow pointing one way: the engine defines the hook
shape and calls it; the app supplies the policy. The types here are deliberately
Cedar-agnostic -- the engine fills domain facts it owns (a library's lifecycle
stage, a node's arbitrary-code flag) and a hook maps them onto whatever policy
model it likes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AuthorizationCheckpoint:
    """One point where the engine asks whether a resolved operation is permitted.

    `action` is the semantic operation (e.g. ``"LoadLibrary"``, ``"InstantiateNode"``).
    `subject_type` / `subject_id` identify the thing being acted on (e.g.
    ``"Library"`` and the library name). `attributes` are the facts the engine
    owns about that subject (e.g. ``{"lifecycle_stage": "EXPERIMENTAL"}``); a hook
    decides which it cares about. The engine fills only what it has resolved; it
    does not know which attributes a policy will read.
    """

    action: str
    subject_type: str
    subject_id: str
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CheckpointFailure:
    """One reason a checkpoint was denied, in a form the engine can render.

    `detail` is a human-readable sentence the engine surfaces directly (in a
    fitness problem, an error-proxy node, or a failure result). `capability` and
    `advice` are optional structured fields a richer UI can render separately
    (the entitlement the user lacks and what to ask their admin for).
    """

    detail: str
    capability: str | None = None
    advice: str | None = None


@dataclass(frozen=True)
class CheckpointDenial:
    """The verdict when a checkpoint is denied: one or more failures to surface.

    A denial carries *every* failure so the engine can show all missing
    permissions at once rather than the first. A hook returns `None` (not an
    empty denial) to allow.
    """

    failures: tuple[CheckpointFailure, ...]

    def messages(self) -> list[str]:
        """The per-failure detail sentences, for plain-text rendering."""
        return [failure.detail for failure in self.failures]
