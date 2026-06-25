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
from enum import StrEnum
from typing import Any


class CheckpointAction(StrEnum):
    """The semantic operations the engine gates at a checkpoint.

    Carried by `AuthorizationCheckpoint.action` so a hook branches on a named
    member rather than a bare string literal. Each value is the wire string the
    app's policy matches on.
    """

    LOAD_LIBRARY = "LoadLibrary"
    INSTANTIATE_NODE = "InstantiateNode"
    INVOKE_MODEL = "InvokeModel"
    LOAD_PROJECT = "LoadProject"
    ACTIVATE_PROJECT = "ActivateProject"


class CheckpointSubjectType(StrEnum):
    """The kinds of subject a checkpoint acts on, paired with `subject_id`."""

    LIBRARY = "Library"
    NODE_TYPE = "NodeType"
    MODEL = "Model"
    PROJECT = "Project"


class CheckpointAttribute(StrEnum):
    """The attribute keys the engine fills on a checkpoint's `attributes`.

    Centralizing the keys keeps the engine's producer side and the app's policy
    hook reading the same names, so a typo cannot silently drop a fact. The engine
    fills only the keys it has resolved for a given checkpoint; a hook reads
    whichever it cares about. Members are `str` values, so they serve as dict keys
    that compare equal to their literal spelling on the reading side.
    """

    ID = "id"
    LIFECYCLE_STAGE = "lifecycle_stage"
    PROVIDER_ID = "provider_id"
    EXECUTES_ARBITRARY_CODE = "executes_arbitrary_code"
    NAME = "name"
    MODEL_IDS = "model_ids"
    PROVIDER_IDS = "provider_ids"
    MODEL_FAMILIES = "model_families"


@dataclass(frozen=True)
class AuthorizationCheckpoint:
    """One point where the engine asks whether a resolved operation is permitted.

    `action` is the semantic operation (e.g. ``CheckpointAction.LOAD_LIBRARY``).
    `subject_type` / `subject_id` identify the thing being acted on (e.g.
    ``CheckpointSubjectType.LIBRARY`` and the library name). `attributes` are the
    facts the engine owns about that subject (e.g.
    ``{CheckpointAttribute.LIFECYCLE_STAGE: "EXPERIMENTAL"}``); a hook decides
    which it cares about. The engine fills only what it has resolved; it does not
    know which attributes a policy will read.
    """

    action: CheckpointAction
    subject_type: CheckpointSubjectType
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

    def reason(self, *, separator: str = "; ", default: str = "Denied by the license policy.") -> str:
        """The denial as one display string: every failure detail joined, or `default`.

        A hook returns `None` (not an empty denial) to allow, so empty `failures` is
        a contract violation; `default` keeps the surfaced message coherent if one
        slips through. Callers pass `separator` to match their surrounding format
        (inline `"; "` in a failure result, a newline in a multi-line node error).
        """
        return separator.join(self.messages()) or default
