"""BudgetManager - Describes which project an outbound call should be attributed to.

Holds no state: the project chain is read from the project manager per request, so a project
switch between two calls shows up on the second. No network call, no credential, no enforcement.

**Nothing here judges a value.** Griptape Cloud's parser reports every degradation it applies
and marks a chain it repaired `mangled`, which drops it out of budget matching. A value the
engine repaired instead arrives looking intact -- nothing recorded, and a budget matched against
a rewritten string. So ids travel exactly as stored and the far end decides.

Keeping the header out of logs and broadcasts is a convention, not an invariant:
`broadcast_result` is a field any caller can flip, post-dispatch hooks see the full result, and
worker forwarding puts the Success payload on the response topic by construction. What this
module can do it does -- `header_value` never reaches `result_details`, which is logged.

`Engine.handle_request` turns a raised handler exception into a `ResultPayloadFailure`, so there
is no blanket try/except here; the resolver guards its own peer read and reports the failure
rather than swallowing it.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import TYPE_CHECKING, Any

from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.base_events import ResultDetails
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

    `project` is the only key the Cloud matches budgets against, so it is the only one sent, and
    `tags` is the only namespace the parser reads.

    An empty chain omits `tags` rather than sending it empty, and the bare `{"v": 1}` still goes
    out -- but only because it is harmless and forward-compatible, not because the far end can
    read anything off it. `Attribution()` defaults `tags_read` and `interpreted` to True, so an
    absent header satisfies every conjunct of `project_chain_absent` exactly as this envelope
    does; the two parse to equal objects and neither emits a metric. If the Cloud ever adds a
    `client_attributed` flag this becomes a real signal without a client release.
    """
    if not project_chain:
        return {"v": ATTRIBUTION_SCHEMA_VERSION}
    return {"v": ATTRIBUTION_SCHEMA_VERSION, "tags": {"project": list(project_chain)}}


def _encode_attribution_payload(payload: dict[str, Any]) -> str | None:
    """Encode a payload as base64url, or None when it cannot be encoded at all.

    Size is not judged, here or anywhere: an oversized header is discarded *and reported* by the
    Cloud, which beats shrinking it here and having it arrive looking complete. Padding is kept
    because the parser re-pads anyway, so both forms arrive intact.

    A legacy project's id is the path to its file, and a path whose bytes are not valid UTF-8
    carries lone surrogates from `surrogateescape` that `str.encode` refuses. That costs the whole
    header: it is the one thing the wire cannot carry, so there is no honest partial form.

    Returning None rather than raising, because a handler exception becomes a
    `GenericResultFailure`, which ignores `failure_log_level` (`event_manager.py:1088-1094`).

    This warning is the engineer-facing half of the pair and the artist-facing message is the
    other; the traceback stays because `UnicodeEncodeError` names the offending codepoint and its
    position, which is the only pointer to which entry of a deep chain is the bad one.
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
    """Composes the attribution context for a credit-consuming call."""

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
        """Describe the current project as an encoded attribution header.

        Both failures send no header at all rather than the `{"v": 1}` they used to. The far end
        cannot tell those apart -- an absent header and a bare envelope parse to equal objects --
        so this buys nothing on the wire. It is about the engine not asserting a fact it does not
        have, and about the caller getting a Failure it can act on instead of a Success carrying
        an empty chain. Neither blocks the call: it is about to spend money, and an unattributed
        call beats a blocked one.

        Both are logged at WARNING rather than the ERROR a bare `result_details` string would
        default to. Neither condition clears on its own -- a legacy project keeps its unencodable
        id -- so an ERROR would repeat once per metered call for something the artist cannot act
        on and that did not stop the work.
        """
        project_chain = self._resolve_project_chain()
        if project_chain is None:
            return GetAttributionContextResultFailure(
                result_details=ResultDetails(
                    message=(
                        "Attempted to describe an outbound request for budget attribution. Failed because the "
                        "current project's ancestry could not be read. The request can proceed, but this spend "
                        "will not be attributed."
                    ),
                    level=logging.WARNING,
                )
            )

        header_value = _encode_attribution_payload(_build_attribution_payload(project_chain))
        if header_value is None:
            return GetAttributionContextResultFailure(
                result_details=ResultDetails(
                    message=(
                        "Attempted to describe an outbound request for budget attribution. Failed because a "
                        "project id contains characters that cannot be sent in a request header. The request "
                        "can proceed, but this spend will not be attributed."
                    ),
                    level=logging.WARNING,
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

    def _resolve_project_chain(self) -> list[str] | None:
        """Resolve the current project's ancestry as ids, leaf-first, or None when unreadable.

        None and [] are different answers and must not collapse. [] means no project is open;
        a peer that raised knows nothing of the kind. The far end cannot tell the two apart --
        a bare `{"v": 1}` and no header at all parse to equal objects -- so this buys nothing
        on the wire. It buys the engine not asserting a fact it does not have, and a caller
        that gets a Failure it can act on instead of a Success carrying an empty chain.

        The id rather than the name, because it survives a rename -- a name would re-point that
        project's spend the moment a user edited it. But an id is not opaque: `ProjectTemplate.id`
        has no pattern and no length cap, the editor proposes a slug of the name, and a legacy
        project carries the canonical path to its file. So an id discloses roughly what a name
        would plus directory layout, in a header egress proxies log. Accepted for a stable key.

        Ids travel exactly as stored. The one entry skipped is the `<system-defaults>` rest-state
        sentinel, which is not an ancestor the Cloud models and whose string it reserves anyway.

        Matched on the stripped id, because that is the form the far end matches on: it strips
        each entry before testing the reserved value, so a padded copy is the same claim to it.
        Left whole, such an entry is dropped there rather than here -- and a dropped entry
        promotes its parent to leaf while `ENTRY_DROPPED` stays out of `mangled`, so the short
        chain still matches a budget and bills a real ancestor. Skipping an entry is not the
        value-repair the verbatim rule forbids; every id that does travel still travels untouched.

        Not covered: an id long enough that the far end's 256-character cut leaves exactly the
        reserved string. Catching that means mirroring `MAX_VALUE_LENGTH` here, which is the
        constant-mirroring this module deleted on purpose. The real fix is on the far end --
        `ENTRY_DROPPED` belongs in `mangled`.
        """
        try:
            chain = self.engine.project_manager.get_project_chain()
        except Exception:
            logger.warning("Could not resolve the project chain for budget attribution.", exc_info=True)
            return None

        return [entry.id for entry in chain if entry.id.strip() != SYSTEM_DEFAULTS_KEY]
