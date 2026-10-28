"""Requests for the output destination a settings node supplies to a connected parameter.

A settings node such as File Output Settings hands its destination to the parameter it is wired
to. The destination is read where the settings node lives and sent back as plain data, so a node
running in a worker gets the same destination as one running in-process.
"""

from __future__ import annotations

from dataclasses import dataclass

from griptape_nodes.retained_mode.events.base_events import (
    GraphAccessMixin,
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.os_events import ExistingFilePolicy
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry
from griptape_nodes.retained_mode.events.project_events import MacroPath
from griptape_nodes.retained_mode.file_metadata.sidecar_metadata import SidecarContent


@dataclass
class FileDestinationSpec:
    """A ``FileDestination`` as plain data.

    Args:
        map_to_project: True for a ``ProjectFileDestination``, which maps each written path
            back to its project macro form.
    """

    file_path: str | MacroPath
    existing_file_policy: ExistingFilePolicy
    append: bool
    create_parents: bool
    coerce_extension_to_match_bytes: bool
    file_metadata: SidecarContent | None
    map_to_project: bool


@dataclass
class DirectoryDestinationSpec:
    """A ``DirectoryDestination`` as plain data."""

    dir_path: str | MacroPath
    existing_dir_policy: ExistingFilePolicy
    create_parents: bool


@dataclass
class FileSequenceDestinationSpec:
    """A ``FileSequenceDestination`` as plain data, with its version index already in ``macro_path``."""

    macro_path: MacroPath
    existing_file_policy: ExistingFilePolicy
    create_parents: bool


@dataclass
@PayloadRegistry.register
class GetConnectedFileDestinationRequest(RequestPayload, GraphAccessMixin):
    """Get the file destination a settings node connected to a parameter supplies.

    Use when: A node is about to write a file and its output parameter may be wired to a
    File Output Settings node.

    Args:
        node_name: Name of the node that owns the parameter
        parameter_name: Name of the output parameter the settings node is connected to

    Results: GetConnectedFileDestinationResultSuccess (destination is None when no settings node
        is connected) | GetConnectedFileDestinationResultFailure (node or parameter not found, or
        the connected settings node has no destination configured)
    """

    node_name: str
    parameter_name: str


@dataclass
@PayloadRegistry.register
class GetConnectedFileDestinationResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    destination: FileDestinationSpec | None


@dataclass
@PayloadRegistry.register
class GetConnectedFileDestinationResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    pass


@dataclass
@PayloadRegistry.register
class GetConnectedDirectoryDestinationRequest(RequestPayload, GraphAccessMixin):
    """Get the directory destination a settings node connected to a parameter supplies.

    Args:
        node_name: Name of the node that owns the parameter
        parameter_name: Name of the output parameter the settings node is connected to

    Results: GetConnectedDirectoryDestinationResultSuccess (destination is None when no settings
        node is connected) | GetConnectedDirectoryDestinationResultFailure (node or parameter not
        found, or the connected settings node has no destination configured)
    """

    node_name: str
    parameter_name: str


@dataclass
@PayloadRegistry.register
class GetConnectedDirectoryDestinationResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    destination: DirectoryDestinationSpec | None


@dataclass
@PayloadRegistry.register
class GetConnectedDirectoryDestinationResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    pass


@dataclass
@PayloadRegistry.register
class GetConnectedFileSequenceDestinationRequest(RequestPayload, GraphAccessMixin):
    """Get the file sequence destination a settings node connected to a parameter supplies.

    Args:
        node_name: Name of the node that owns the parameter
        parameter_name: Name of the output parameter the settings node is connected to

    Results: GetConnectedFileSequenceDestinationResultSuccess (destination is None when no
        settings node is connected) | GetConnectedFileSequenceDestinationResultFailure (node or
        parameter not found, or the connected settings node has no destination configured)
    """

    node_name: str
    parameter_name: str


@dataclass
@PayloadRegistry.register
class GetConnectedFileSequenceDestinationResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    destination: FileSequenceDestinationSpec | None


@dataclass
@PayloadRegistry.register
class GetConnectedFileSequenceDestinationResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    pass
