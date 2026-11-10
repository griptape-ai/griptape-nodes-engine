"""Pub/sub topic names that engines, apps and clients share. Callers supply every id.

| Topic | Carries |
|---|---|
| `request` | requests addressed to no engine in particular |
| `sessions/<id>/request` | requests to the engine that holds the session |
| `sessions/<id>/response` | app events, and results and events that name no workflow |
| `sessions/<id>/clients/<id>/response` | the results of one client's own requests |
| `clients/<id>/response` | the same, for a client that knows no session, such as a `RequestClient` given none |
| `sessions/<id>/workflows/<id>/events` | one open workflow's broadcast results and execution events |
| `sessions/<id>/workers/<id>/request` | requests to one worker engine of the session |
| `sessions/<id>/workers/<id>/response` | that worker's results |
| `engines/<id>/request` | requests addressed to one engine |
| `engines/<id>/response` | replies to one engine's requests, or the session topic's traffic with no session |
"""

from __future__ import annotations

# Topic the MCP server and other API clients send requests on, addressed to no engine in particular.
REQUEST_TOPIC = "request"


def session_request_topic(session_id: str) -> str:
    """Topic the session owner (orchestrator) receives requests on."""
    return f"sessions/{session_id}/request"


def session_response_topic(session_id: str) -> str:
    """Topic everyone on the session hears: app events, and results and events that name no workflow."""
    return f"sessions/{session_id}/response"


def client_response_topic(session_id: str | None, client_id: str) -> str:
    """Topic one client takes the results of its own requests on: under its session, or alone with None."""
    if session_id:
        return f"sessions/{session_id}/clients/{client_id}/response"
    return f"clients/{client_id}/response"


def workflow_events_topic(engine_id: str | None, session_id: str | None, open_workflow_id: str) -> str:
    """Topic the broadcast results and execution events of one open workflow go out on.

    Under the session when there is one, else under the engine, like `default_response_topic`.
    """
    if session_id:
        return f"sessions/{session_id}/workflows/{open_workflow_id}/events"
    if engine_id:
        return f"engines/{engine_id}/workflows/{open_workflow_id}/events"
    return f"workflows/{open_workflow_id}/events"


def engine_request_topic(engine_id: str) -> str:
    """Topic one engine receives directly-addressed requests on."""
    return f"engines/{engine_id}/request"


def engine_response_topic(engine_id: str) -> str:
    """Topic one engine's results are addressed to."""
    return f"engines/{engine_id}/response"


def engine_topic_prefix(engine_id: str) -> str:
    """The prefix of every topic addressed to one engine, such as replies to its requests."""
    return f"engines/{engine_id}/"


def worker_request_topic(session_id: str | None, worker_engine_id: str) -> str:
    """Topic one worker engine of the session receives requests on."""
    return f"sessions/{session_id}/workers/{worker_engine_id}/request"


def worker_response_topic(session_id: str | None, worker_engine_id: str) -> str:
    """Topic one worker engine of the session sends its results on."""
    return f"sessions/{session_id}/workers/{worker_engine_id}/response"


def worker_engine_id_of_topic(topic: str) -> str | None:
    """The worker a session worker topic (`sessions/<session>/workers/<worker>/...`) names, or None for any other topic."""
    match topic.split("/"):
        case ["sessions", _session_id, "workers", worker_engine_id, _, *_] if worker_engine_id:
            return worker_engine_id
        case _:
            return None


def engine_id_of_response_topic(topic: str) -> str | None:
    """The engine an `engine_response_topic` names, or None for any other topic."""
    prefix, _, rest = topic.partition("/")
    engine_id, _, suffix = rest.partition("/")
    if prefix == "engines" and suffix == "response" and engine_id:
        return engine_id
    return None


def default_response_topic(engine_id: str | None, session_id: str | None) -> str:
    """Topic everyone on the session hears, or with no session, the engine's response topic."""
    if session_id:
        return session_response_topic(session_id)
    if engine_id:
        return engine_response_topic(engine_id)
    return "response"


def audience_topic(engine_id: str | None, session_id: str | None, open_workflow_id: str | None) -> str:
    """Topic the clients following `open_workflow_id` hear its broadcasts on, or with None, `default_response_topic`."""
    if open_workflow_id is None:
        return default_response_topic(engine_id, session_id)
    return workflow_events_topic(engine_id, session_id, open_workflow_id)
