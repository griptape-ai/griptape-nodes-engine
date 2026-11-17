"""What each engine request type acts on, as its handler's scope declares (`RequestScope`).

`data/request_scope_golden.json` is shared: the editor copies it to type the requests it sends,
so a request that acts on a workflow can only be sent naming one. A handler whose scope changes,
or a request type added or removed, fails here until the golden is updated, and then in the
editor until its copy is.

Regenerate with `GTN_UPDATE_REQUEST_SCOPE_GOLDEN=1 uv run pytest <this file>`.
"""

import json
import os
from pathlib import Path

from griptape_nodes.retained_mode.engine import current_engine
from griptape_nodes.retained_mode.request_handlers import RequestScope
from griptape_nodes.retained_mode.unmigrated_requests import ONLY_OPEN_UNTIL_CLIENTS_NAME

GOLDEN_PATH = Path(__file__).parent / "data" / "request_scope_golden.json"
UPDATE_ENV_VAR = "GTN_UPDATE_REQUEST_SCOPE_GOLDEN"


def _request_scopes() -> dict[str, list[str]]:
    """Every request type the engine handles, by the scope its handler declares."""
    event_manager = current_engine().event_manager
    scopes: dict[str, list[str]] = {scope.value: [] for scope in RequestScope}
    for request_type in event_manager.registered_request_types():
        scope = event_manager.get_scope_for_request_type(request_type)
        # Engine handlers all declare a scope. Only a library handler goes without, and none is loaded here.
        assert scope is not None, f"{request_type.__name__} has no scope"
        scopes[scope.value].append(request_type.__name__)
    golden = {name: sorted(types) for name, types in scopes.items()}
    golden["only_open_until_clients_name"] = sorted(ONLY_OPEN_UNTIL_CLIENTS_NAME)
    return golden


def test_the_golden_lists_every_request_type_by_scope() -> None:
    """A handler whose scope changes, or a request type added or removed, changes the golden."""
    scopes = _request_scopes()
    if os.environ.get(UPDATE_ENV_VAR):
        GOLDEN_PATH.write_text(json.dumps(scopes, indent=2) + "\n", encoding="utf-8")

    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

    assert scopes == golden, (
        f"Request scopes changed. Regenerate with {UPDATE_ENV_VAR}=1 and copy the golden to the editor."
    )


def test_only_open_until_clients_name_lists_only_requests_that_may_name_no_workflow() -> None:
    """The fallback applies to OPTIONAL_WORKFLOW requests, so the editor treats those as workflow requests."""
    scopes = _request_scopes()

    assert set(scopes["only_open_until_clients_name"]) <= set(scopes[RequestScope.OPTIONAL_WORKFLOW.value])
