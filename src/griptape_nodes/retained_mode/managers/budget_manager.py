"""BudgetManager - Describes the engine context an outbound call should be attributed to.

The manager owns no state. The project chain is read from a peer manager at request time, so a
project switch between two calls is reflected on the second one. Nothing here makes a network
call, holds a credential, or enforces a budget.

**Keeping the header value out of logs and broadcasts is a convention, not an invariant.**
`broadcast_result` defaults to False but is a per-instance field any caller can flip;
post-dispatch hooks receive the full result regardless of it; a forwarded worker request
(`app/worker_routing.FORWARDED_REQUEST_TYPES`) puts the Success payload on the worker
response topic by construction; and the engine's `omit_from_result` redaction primitive is
request-side only, so it cannot mark a result field non-broadcastable. What this module can
do it does: it never interpolates `header_value` or the payload into `result_details`, which
is logged as well as broadcast. Project ids are descriptive rather than secret, so none of that
is alarming -- but it is the real state of the mechanism.

**Nothing here judges a value.** Griptape Cloud's parser already reports every degradation it
applies -- a truncated value, a truncated chain, a dropped entry -- and marks a chain it had to
repair `mangled`, which takes it out of budget matching entirely. Repairing or withholding a
value here would arrive looking intact instead: no reason recorded, no metric fired, and a
budget matched against something the engine quietly rewrote. So ids travel exactly as the
project manager reports them, at whatever length that comes to, and the far end decides.

`Engine.handle_request` converts a raised handler exception into a `ResultPayloadFailure`,
so there is deliberately no blanket try/except here; the resolver guards its own peer read.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import TYPE_CHECKING, Any

from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.budget_events import (
    ATTRIBUTION_SCHEMA_VERSION,
    GetAttributionContextRequest,
    GetAttributionContextResultFailure,
    GetAttributionContextResultSuccess,
)
from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.managers.event_manager import EventManager

logger = logging.getLogger("griptape_nodes")


def _build_attribution_payload(project_chain: list[str]) -> dict[str, Any]:
    """Build the decoded payload for a project chain, which may be empty.

    `project` is the only tag key the engine sends, because it is the only one Griptape Cloud
    matches budgets against. It lives under `tags`; the parser reads no other namespace.

    An empty chain omits `tags` entirely rather than sending it empty, and the bare `{"v": 1}`
    envelope that leaves is worth sending: the parser reads a missing `tags` as "no project is
    open on a client that speaks attribution", which is a different and more useful fact than
    sending no header at all, which it reads as "this client has not adopted attribution".
    """
    if not project_chain:
        return {"v": ATTRIBUTION_SCHEMA_VERSION}
    return {"v": ATTRIBUTION_SCHEMA_VERSION, "tags": {"project": list(project_chain)}}


def _encode_attribution_payload(payload: dict[str, Any]) -> str | None:
    """Encode a payload as base64url, or None when it cannot be encoded at all.

    Size is not judged here, or anywhere. A header the Cloud considers oversized is discarded
    *and reported* as OVERSIZE, so the spend goes unattributed and the platform knows it did --
    a better outcome than shrinking the header here and having it arrive looking complete.

    Padding is kept because the payload is `base64.b64encode`'s natural output and stripping it
    would buy nothing: the parser re-pads whatever it receives before decoding, so both forms
    arrive intact.

    The UTF-8 guard is the module's only failure path, and it fires by design rather than as a
    backstop. A project id derived from a filesystem path whose bytes are not valid UTF-8 comes
    back from the OS carrying lone surrogates from `surrogateescape`, which `str.encode` refuses.
    That costs the whole header, deliberately: it is the one thing the wire physically cannot
    carry, so there is no honest partial form to send instead. Letting `str.encode` raise would
    escape the handler as a `GenericResultFailure`, which ignores `failure_log_level`
    (`event_manager.py:1088-1094`) and would log an ERROR with a traceback on every metered call.
    """
    try:
        raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        logger.warning(
            "Attribution payload could not be encoded as UTF-8; sending no attribution header.",
            exc_info=True,
        )
        return None
    return base64.urlsafe_b64encode(raw).decode("ascii")


class BudgetManager(EngineScoped):
    """Composes the attribution context for a credit-consuming call.

    Holds no state: the project chain is read from the project manager on every request, so the
    answer always reflects the engine as it is now.
    """

    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        """Initialize the BudgetManager.

        Args:
            event_manager: The EventManager instance to use for event handling.
            engine: The owning Engine, used to resolve peer managers.
        """
        super().__init__(engine)
        event_manager.assign_manager_to_request_type(
            GetAttributionContextRequest, self.on_get_attribution_context_request
        )

    def on_get_attribution_context_request(
        self,
        request: GetAttributionContextRequest,  # noqa: ARG002
    ) -> GetAttributionContextResultSuccess | GetAttributionContextResultFailure:
        """Describe the current engine context as an encoded attribution header.

        A project chain the engine cannot resolve degrades to an empty one rather than a
        failure: the caller is about to spend money, and an unattributed call beats a blocked
        one. The one failure is an id the wire cannot carry at all.
        """
        project_chain = self._resolve_project_chain()
        header_value = _encode_attribution_payload(_build_attribution_payload(project_chain))
        if header_value is None:
            return GetAttributionContextResultFailure(
                result_details=(
                    "Attempted to describe an outbound request for budget attribution. Failed because a "
                    "project id contains characters that cannot be sent in a request header. The request "
                    "can proceed, but this spend will not be attributed."
                )
            )

        return GetAttributionContextResultSuccess(
            header_value=header_value,
            project_chain=list(project_chain),
            result_details=(
                f"Successfully described the attribution context for an outbound request "
                f"({len(project_chain)} project(s) in the chain)."
            ),
        )

    def _resolve_project_chain(self) -> list[str]:
        """Resolve the current project's ancestry as ids, leaf-first, or [] when unavailable.

        `tags.project` is the only dimension the Cloud matches budgets against, and the project
        id is what identifies a project across a rename -- the name would silently re-point that
        project's spend the moment a user edited it.

        An id is not an opaque GUID. `ProjectTemplate.id` has no pattern and no length cap: the
        editor proposes a slug of the project name, the user may replace it with anything, and a
        legacy project without one has the canonical path to its file written in permanently. So
        an id discloses roughly what a name would, plus directory layout, in a header that
        SSL-inspecting egress proxies log. That is the accepted cost of a stable identifier.

        Every id travels exactly as it is stored -- unstripped, uncut, unexamined. The one entry
        skipped is the `<system-defaults>` rest-state sentinel, and that is not a repair: it is
        the root sentinel rather than an ancestor the Cloud models, and the Cloud reserves that
        string for its own default and refuses a client copy of it.
        """
        try:
            chain = self.engine.project_manager.get_project_chain()
        except Exception:
            logger.warning("Could not resolve the project chain for budget attribution.", exc_info=True)
            return []

        return [entry.id for entry in chain if entry.id != SYSTEM_DEFAULTS_KEY]
