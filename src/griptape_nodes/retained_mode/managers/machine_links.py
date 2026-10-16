"""Connections between orchestrators on different machines, as WorkerManager uses them.

An orchestrator whose distribution settings send a library's nodes to another machine has that
machine's orchestrator start the library's worker in the originator's session. The worker then
registers with the originator like a local worker, and every frame between the two passes through
the serving orchestrator, which relays it without handling it.
"""

from __future__ import annotations

from typing import Protocol

# The version of the requests orchestrators on different machines exchange. While it is 0.x, any
# difference is refused, since both machines also have to run the same engine version.
DISTRIBUTED_EXECUTION_SCHEMA_VERSION = "0.2.0"


class MachineNotFoundError(Exception):
    """Raised when a machine cannot be reached, carrying why, written for an artist."""


class MachineLinks(Protocol):
    """The transport operations an orchestrator needs to reach, and to serve, other machines.

    A machine is named by the engine ID of its orchestrator, as written in the distribution settings.
    """

    async def connect(self, machine: str) -> None:
        """Find the machine through the engine registry and link to it, or follow it to a new address.

        Raises:
            MachineNotFoundError: The registry has no running engine `machine` that this one can reach.
        """
        ...

    async def publish(self, machine: str, event_type: str, payload: str, topic: str) -> None:
        """Send a frame to the orchestrator of `machine`."""
        ...

    async def subscribe(self, machine: str, topic: str) -> None:
        """Receive the frames published on `topic` at the orchestrator of `machine`."""
        ...

    async def route(self, machine: str, topic_prefix: str) -> None:
        """Also send this engine's broadcasts on topics under `topic_prefix` to `machine`."""
        ...

    async def unroute(self, machine: str, topic_prefix: str) -> None:
        """Stop sending this engine's broadcasts on topics under `topic_prefix` to `machine`."""
        ...

    async def relay(self, topic_prefix: str) -> None:
        """Pass frames clients publish under `topic_prefix` to the other subscribed clients.

        Such frames reach the other side without this engine handling them, which is how a serving
        orchestrator carries traffic between a worker and the originator it serves.
        """
        ...

    async def unrelay(self, topic_prefix: str) -> None:
        """Stop passing frames clients publish under `topic_prefix` to the other clients."""
        ...


def session_topic_prefix(session_id: str) -> str:
    """The prefix of every topic in `session_id`: its requests, responses, and its workers' topics."""
    return f"sessions/{session_id}/"


def engine_topic_prefix(engine_id: str) -> str:
    """The prefix of every topic addressed to the engine `engine_id`, such as replies to its requests."""
    return f"engines/{engine_id}/"
