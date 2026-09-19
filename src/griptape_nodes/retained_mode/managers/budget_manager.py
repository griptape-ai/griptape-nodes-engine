"""BudgetManager - Describes which project an outbound call should be attributed to.

Holds no state: the project chain is read from the project manager per request, so a project
switch between two calls shows up on the second. No network call, no credential, no enforcement.

**Nothing here judges a value.** Ids travel exactly as stored and the far end decides what to
do with them, because the Cloud reports every degradation it applies, where a value the engine
rewrote would arrive looking intact.

Keeping the header out of logs and broadcasts is a convention, not an invariant:
`broadcast_result` is a field any caller can flip, post-dispatch hooks see the full result, and
worker forwarding puts the Success payload on the response topic by construction. What this
module can do it does -- `header_value` never reaches `result_details`, which is logged.
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

    `project` is the only key the Cloud matches budgets against, and `tags` the only namespace
    its parser reads, so nothing else is sent.

    An empty chain omits `tags`, and the bare `{"v": 1}` still goes out. The far end cannot tell
    that from no header at all, so it travels for forward-compatibility, not as a signal.
    """
    if not project_chain:
        return {"v": ATTRIBUTION_SCHEMA_VERSION}
    return {"v": ATTRIBUTION_SCHEMA_VERSION, "tags": {"project": list(project_chain)}}


def _encode_attribution_payload(payload: dict[str, Any]) -> str | None:
    """Encode a payload as base64url, or None when it cannot be encoded at all.

    Size is not judged: an oversized header is discarded *and reported* by the Cloud, which beats
    shrinking it here and having it arrive looking complete. Padding is kept; the parser re-pads.

    A legacy project's id is the path to its file, and a path whose bytes are not valid UTF-8
    carries lone surrogates from `surrogateescape` that `str.encode` refuses. That costs the whole
    header: the wire cannot carry them, so there is no honest partial form.

    Returns None rather than raising, because a handler exception becomes a `GenericResultFailure`,
    which ignores `failure_log_level`. The traceback stays: `UnicodeEncodeError` names the
    offending codepoint and position, the only pointer to which entry of a deep chain is bad.
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
        cannot tell those apart, so this buys nothing on the wire: it is about the engine not
        asserting a project state it does not know, and about the caller getting a Failure it can
        act on instead of a Success carrying an empty chain. Neither blocks the call -- it is
        about to spend money, and an unattributed call beats a blocked one.

        Both log at WARNING rather than the ERROR a bare `result_details` string would default to.
        Neither condition clears on its own -- a legacy project keeps its unencodable id -- so an
        ERROR would repeat once per metered call for something the artist cannot act on and that
        did not stop the work.
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
        """Resolve the current project's ancestry as ids, leaf-first.

        Returns None when the chain cannot be read and `[]` when no project is open. The two are
        different answers and must not collapse, for the reason the handler above gives.

        Ids rather than names: an id survives a rename, where a name would re-point that project's
        spend the moment a user edited it. It is not opaque, though -- usually a slug of the
        project name, and the path to its file on a legacy project -- so this discloses roughly
        what a name would, plus directory layout, in a header egress proxies log.

        The `<system-defaults>` sentinel is skipped, matched on the stripped id because that is the
        form the far end matches on. An id long enough that the far end's own cut leaves exactly
        the reserved string is not caught here: that would mean mirroring a far-end constant, which
        is the coupling this module deleted on purpose.
        """
        try:
            chain = self.engine.project_manager.get_project_chain()
        except Exception:
            logger.warning("Could not resolve the project chain for budget attribution.", exc_info=True)
            return None

        return [entry.id for entry in chain if entry.id.strip() != SYSTEM_DEFAULTS_KEY]
