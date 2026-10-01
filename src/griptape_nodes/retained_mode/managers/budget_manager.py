"""BudgetManager - Describes which project an outbound call should be attributed to.

Holds no state: the project chain is read from the project manager per request, so a project
switch between two calls shows up on the second. No network call, no credential, no enforcement.

Project ids travel exactly as stored -- unstripped, uncut, never repaired.

The header value stays out of `result_details`, which is logged. That is all this module can do
about disclosure: `broadcast_result` is a field any caller can flip, post-dispatch hooks see the
full result, and worker forwarding puts the Success payload on the response topic.
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

    An empty chain omits `tags` and the bare `{"v": 1}` still goes out, for forward-compatibility.
    """
    if not project_chain:
        return {"v": ATTRIBUTION_SCHEMA_VERSION}
    return {"v": ATTRIBUTION_SCHEMA_VERSION, "tags": {"project": list(project_chain)}}


def _encode_attribution_payload(payload: dict[str, Any]) -> str | None:
    """Encode a payload as base64url with padding kept, or None when it cannot be encoded.

    A project id derived from a filesystem path whose bytes are not valid UTF-8 holds lone
    surrogates that `str.encode` refuses. There is no partial form to fall back to, so the whole
    header is given up.

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

        Both failures send no header rather than a bare `{"v": 1}`, so the caller gets a Failure
        it can act on instead of a Success carrying an empty chain. Neither blocks the call: it is
        about to spend money, and an unattributed call beats a blocked one.

        Both log at WARNING rather than the ERROR a bare `result_details` string would default to.
        Neither condition clears on its own, so an ERROR would repeat once per metered call for
        something the artist cannot act on and that did not stop the work.
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

        Returns None when the chain cannot be read and `[]` when no project is open; the two are
        different answers and must not collapse, for the reason the handler above gives.

        Ids rather than names, because an id survives a rename where a name would re-point that
        project's spend. The `<system-defaults>` sentinel is not a project and is skipped, matched
        on the stripped id.
        """
        try:
            chain = self.engine.project_manager.get_project_chain()
        except Exception:
            logger.warning("Could not resolve the project chain for budget attribution.", exc_info=True)
            return None

        return [entry.id for entry in chain if entry.id.strip() != SYSTEM_DEFAULTS_KEY]
