"""Answer requests for the documentation a library ships.

See docs/development/designs/library_documentation.md.
"""

from __future__ import annotations

import logging
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.node_library.library_registry import Library, LibraryRegistry
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.library_documentation_events import (
    GetLibraryDocumentationPageRequest,
    GetLibraryDocumentationPageResultFailure,
    GetLibraryDocumentationPageResultSuccess,
    GetNodeDocumentationRequest,
    GetNodeDocumentationResultFailure,
    GetNodeDocumentationResultSuccess,
    ListLibraryDocumentationRequest,
    ListLibraryDocumentationResultFailure,
    ListLibraryDocumentationResultSuccess,
)
from griptape_nodes.retained_mode.events.library_events import (
    DescribeNodeTypeRequest,
    DescribeNodeTypeResultSuccess,
    ParameterDescription,
)
from griptape_nodes.retained_mode.managers.library.documentation_index import (
    DocsPageError,
    LibraryDocs,
    ParsedPage,
    page_title,
    read_markdown_file,
)
from griptape_nodes.retained_mode.request_handlers import handles

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.managers.library.common import LibraryInfo
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

logger = logging.getLogger("griptape_nodes")

_MAX_NAMES_SHOWN = 15


@dataclass
class _TextSlice:
    """Part of a page's text, with how long the whole page is."""

    text: str
    total_chars: int
    truncated: bool


@dataclass
class _NodeTarget:
    """The node type a request is about, the library it came from, and a node's own parameters if it named one."""

    node_type: str
    library: str | None
    live_parameters: list[ParameterDescription] | None


@dataclass
class _NodeTargetFailure:
    """A node a request named that could not be used, worded for whoever asked."""

    details: str


@dataclass
class _DocsLookupFailure:
    """A library whose docs could not be found, worded for whoever asked."""

    details: str


class LibraryDocumentation(EngineScoped):
    """Reads the docs a library ships and serves them to the editor and to agents."""

    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        # One LibraryDocs per loaded library. Loading a library again registers a new Library object,
        # which has no entry here, so a refresh rebuilds the index and an unloaded library's entry goes
        # away on its own.
        self._docs_by_library: weakref.WeakKeyDictionary[Library, LibraryDocs] = weakref.WeakKeyDictionary()
        event_manager.register_request_handlers(self)

    @handles(GetNodeDocumentationRequest)
    def get_node_documentation_request(
        self, request: GetNodeDocumentationRequest, workflow: WorkflowRuntime | None = None
    ) -> ResultPayload:
        window = _check_window(request.offset, request.limit)
        if isinstance(window, str):
            return GetNodeDocumentationResultFailure(result_details=window)

        target = _find_node_target(workflow, request)
        if isinstance(target, _NodeTargetFailure):
            return GetNodeDocumentationResultFailure(result_details=target.details)

        described = self.engine.library_manager.catalog.describe_node_type_request(
            DescribeNodeTypeRequest(node_type=target.node_type, library=target.library)
        )
        if not isinstance(described, DescribeNodeTypeResultSuccess) or described.metadata is None:
            return GetNodeDocumentationResultFailure(result_details=described.result_details)

        docs = self._find_docs(described.library)
        if isinstance(docs, _DocsLookupFailure):
            return GetNodeDocumentationResultFailure(result_details=docs.details)

        page = _read_node_page(docs, target.node_type)
        text = _slice_text(page.markdown if page else "", request.offset, request.limit)
        parameters = described.parameters
        if target.live_parameters is not None:
            parameters = target.live_parameters

        markdown = None
        if page is not None:
            markdown = text.text
        assets_root = None
        if docs.docs_root is not None:
            assets_root = str(docs.docs_root)

        metadata = described.metadata
        details = (
            f"Successfully read the documentation for node type '{target.node_type}' in Library '{described.library}'."
        )
        return GetNodeDocumentationResultSuccess(
            library=described.library,
            node_type=target.node_type,
            title=(page.frontmatter_title if page else None) or metadata.display_name,
            markdown=markdown,
            total_chars=text.total_chars,
            truncated=text.truncated,
            description=(page.description if page else None) or metadata.description,
            when_to_use=page.when_to_use if page else None,
            deprecation=metadata.deprecation,
            parameters=parameters,
            assets_root=assets_root,
            result_details=details,
        )

    @handles(ListLibraryDocumentationRequest)
    def list_library_documentation_request(self, request: ListLibraryDocumentationRequest) -> ResultPayload:
        docs = self._find_docs(request.library)
        if isinstance(docs, _DocsLookupFailure):
            return ListLibraryDocumentationResultFailure(result_details=docs.details)

        details = f"Successfully listed the documentation for Library '{request.library}'."
        return ListLibraryDocumentationResultSuccess(
            library=request.library,
            overview_path=docs.overview_path(),
            pages=docs.page_entries(),
            nodes=docs.node_entries(),
            templates=docs.template_entries(),
            skills=docs.skill_entries(),
            result_details=details,
        )

    @handles(GetLibraryDocumentationPageRequest)
    def get_library_documentation_page_request(self, request: GetLibraryDocumentationPageRequest) -> ResultPayload:
        window = _check_window(request.offset, request.limit)
        if isinstance(window, str):
            return GetLibraryDocumentationPageResultFailure(result_details=window)

        docs = self._find_docs(request.library)
        if isinstance(docs, _DocsLookupFailure):
            return GetLibraryDocumentationPageResultFailure(result_details=docs.details)

        try:
            page = docs.read_page(request.path)
        except DocsPageError as err:
            return GetLibraryDocumentationPageResultFailure(result_details=str(err))

        text = _slice_text(page.markdown, request.offset, request.limit)
        assets_root = None
        if docs.docs_root is not None:
            assets_root = str(docs.docs_root)

        details = f"Successfully read the page '{request.path}' in Library '{request.library}'."
        return GetLibraryDocumentationPageResultSuccess(
            library=request.library,
            path=request.path,
            title=page_title(page, Path(request.path).stem),
            markdown=text.text,
            total_chars=text.total_chars,
            truncated=text.truncated,
            description=page.description,
            when_to_use=page.when_to_use,
            assets_root=assets_root,
            result_details=details,
        )

    def _describe_unknown_library(self, library_name: str) -> str:
        """Say that no library has this name, and help the person find the name that works."""
        details = f"Attempted to read the documentation of a Library named '{library_name}'. Failed because no Library with that name was registered."

        registered = sorted(LibraryRegistry.list_libraries())
        if registered:
            shown = ", ".join(f"'{name}'" for name in registered[:_MAX_NAMES_SHOWN])
            more = len(registered) - _MAX_NAMES_SHOWN
            if more > 0:
                shown += f", and {more} more"
            details += f" The registered libraries are: {shown}."

        library_manager = self.engine.library_manager
        for library_path in library_manager.get_libraries_attempted_to_load():
            info = library_manager.get_library_info_for_attempted_load(library_path)
            path = Path(library_path)
            if info.library_name is not None and library_name in (path.name, path.parent.name):
                details += f" '{library_name}' is the folder of the library named '{info.library_name}'. Use that name."
                break
        return details

    def _find_docs(self, library_name: str) -> LibraryDocs | _DocsLookupFailure:
        try:
            library = LibraryRegistry.get_library(name=library_name)
        except KeyError:
            return _DocsLookupFailure(details=self._describe_unknown_library(library_name))

        cached = self._docs_by_library.get(library)
        if cached is not None:
            return cached

        info = self.engine.library_manager.get_library_info_by_library_name(library_name)
        if info is None:
            details = f"Attempted to read the documentation of a Library named '{library_name}'. Failed because the engine does not know where the library is installed."
            return _DocsLookupFailure(details=details)

        docs = LibraryDocs(_library_root(info), library.get_library_data())
        self._docs_by_library[library] = docs
        return docs


def _find_node_target(
    workflow: WorkflowRuntime | None, request: GetNodeDocumentationRequest
) -> _NodeTarget | _NodeTargetFailure:
    """Work out which node type a request is about, reading it from the node when the request names one."""
    if request.node_name is None:
        if request.node_type is None:
            details = "Attempted to read the documentation for a node. Failed because the request named neither a node on the canvas nor a node type."
            return _NodeTargetFailure(details=details)
        return _NodeTarget(node_type=request.node_type, library=request.library, live_parameters=None)

    return _find_live_node_target(workflow, request.node_name, request)


def _find_live_node_target(
    workflow: WorkflowRuntime | None, node_name: str, request: GetNodeDocumentationRequest
) -> _NodeTarget | _NodeTargetFailure:
    """Read the node type and parameters from a node on the canvas."""
    if workflow is None:
        details = f"Attempted to read the documentation for the node '{node_name}'. Failed because no workflow is open."
        return _NodeTargetFailure(details=details)

    node = workflow.objects.get_as_type(node_name, BaseNode)
    if node is None:
        details = f"Attempted to read the documentation for the node '{node_name}'. Failed because no node with that name was found."
        return _NodeTargetFailure(details=details)

    node_type = node.metadata.get("node_type")
    if not isinstance(node_type, str):
        details = f"Attempted to read the documentation for the node '{node_name}'. Failed because the node does not record which node type it was created from."
        return _NodeTargetFailure(details=details)

    if request.node_type is not None and request.node_type != node_type:
        details = f"Attempted to read the documentation for the node '{node_name}'. Failed because it is a '{node_type}' node, not a '{request.node_type}' node."
        return _NodeTargetFailure(details=details)

    library = node.metadata.get("library")
    if not isinstance(library, str):
        library = request.library
    return _NodeTarget(
        node_type=node_type,
        library=library,
        live_parameters=[ParameterDescription.from_parameter(param) for param in node.parameters],
    )


def _read_node_page(docs: LibraryDocs, node_type: str) -> ParsedPage | None:
    """A node's page, or None when the library ships none or the page cannot be read."""
    page_path = docs.node_page(node_type)
    if page_path is None:
        return None
    try:
        return read_markdown_file(page_path)
    except DocsPageError as err:
        logger.warning("Showing node '%s' without its page. %s", node_type, err)
        return None


def _library_root(info: LibraryInfo) -> Path:
    """The folder a library's docs are found from: the manifest's folder, or the sandbox folder itself."""
    path = Path(info.library_path)
    if info.is_sandbox or path.is_dir():
        return path
    return path.parent


def _check_window(offset: int, limit: int | None) -> str | None:
    """Why a request's offset and limit are not usable, or None when they are."""
    if offset < 0:
        return "Attempted to read part of a page. Failed because the offset cannot be negative."
    if limit is not None and limit <= 0:
        return "Attempted to read part of a page. Failed because the limit must be more than zero."
    return None


def _slice_text(text: str, offset: int, limit: int | None) -> _TextSlice:
    end = len(text)
    if limit is not None:
        end = min(end, offset + limit)
    return _TextSlice(text=text[offset:end], total_chars=len(text), truncated=end < len(text))
