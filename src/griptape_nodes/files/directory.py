"""Directory - a macro-path handle for project directories.

Supports I/O-free path inspection and deferred write via DirectoryDestination.
"""

from __future__ import annotations

import pathlib

from griptape_nodes.common import macro_parser
from griptape_nodes.files import file as file_mod
from griptape_nodes.files import project_file
from griptape_nodes.retained_mode import griptape_nodes as griptape_nodes_mod
from griptape_nodes.retained_mode.events import os_events, project_events


class DirectoryError(Exception):
    """Raised when a directory operation fails."""

    def __init__(self, result_details: str) -> None:
        self.result_details = result_details
        super().__init__(result_details)


class Directory:
    """Path-like object representing a directory.

    The constructor stores a directory reference without performing any I/O.
    Call ``resolve()`` to get the absolute filesystem path.

    Supports MacroPath resolution: pass a MacroPath (which contains variables)
    or a plain string path. Plain strings containing macro variables are
    automatically wrapped in a MacroPath.
    """

    def __init__(self, dir_path: str | project_events.MacroPath) -> None:
        """Store directory reference. No I/O is performed.

        Args:
            dir_path: Path to the directory. Can be a plain string or a MacroPath.
        """
        if isinstance(dir_path, str):
            try:
                parsed = macro_parser.ParsedMacro(dir_path)
            except macro_parser.MacroSyntaxError:
                self._dir_path: str | project_events.MacroPath = dir_path
            else:
                if parsed.get_variables():
                    self._dir_path = project_events.MacroPath(parsed, {})
                else:
                    self._dir_path = dir_path
        else:
            self._dir_path = dir_path

    def resolve(self) -> pathlib.Path:
        """Resolve and return the absolute path for this directory.

        Returns:
            Absolute Path object.

        Raises:
            DirectoryError: If macro resolution fails (e.g. no project loaded).
        """
        return pathlib.Path(_resolve_dir_path(self._dir_path))

    @property
    def location(self) -> str:
        """Return the most portable string representation of this directory's location.

        Returns the macro template when the directory holds a macro path,
        otherwise the plain path string. No I/O is performed.
        """
        if isinstance(self._dir_path, project_events.MacroPath):
            return self._dir_path.parsed_macro.template
        return self._dir_path

    @property
    def name(self) -> str:
        """Return the directory name (last path component)."""
        return pathlib.Path(self.location).name


class DirectoryDestination:
    """A pre-configured handle for directory creation.

    Bundles a directory path with a creation policy so it can be passed
    around as a self-contained object. The consumer calls ``create()``
    without needing to know the policy details.

    When the policy is CREATE_NEW and the path contains a ``{_index}``
    macro variable, ``create()`` increments ``_index`` starting at 1 until
    it finds a path that does not yet exist, then creates that directory.
    This produces versioned directories: ``renders_v001/``, ``renders_v002/``.
    """

    def __init__(
        self,
        dir_path: str | project_events.MacroPath,
        *,
        existing_dir_policy: os_events.ExistingFilePolicy = os_events.ExistingFilePolicy.CREATE_NEW,
        create_parents: bool = True,
    ) -> None:
        """Store directory path and creation configuration. No I/O is performed.

        Args:
            dir_path: Path to the directory. Can be a plain string or a MacroPath.
            existing_dir_policy: How to handle an existing directory.
                CREATE_NEW increments _index; OVERWRITE allows reuse; FAIL raises.
                Defaults to CREATE_NEW.
            create_parents: If True, create intermediate directories automatically.
                Defaults to True.
        """
        self._directory = Directory(dir_path)
        self._dir_path = dir_path
        self._existing_dir_policy = existing_dir_policy
        self._create_parents = create_parents

    def resolve(self) -> str:
        """Resolve and return the absolute path for this destination.

        Returns:
            Absolute path string.

        Raises:
            DirectoryError: If macro resolution fails.
        """
        return _resolve_dir_path(self._dir_path)

    @property
    def location(self) -> str:
        """Return the most portable string representation of this destination's location."""
        if isinstance(self._dir_path, project_events.MacroPath):
            return self._dir_path.parsed_macro.template
        return self._dir_path

    def create(self) -> Directory:
        """Create the directory and return a Directory referencing it.

        When policy is CREATE_NEW and the path is a MacroPath containing
        ``{_index}``, increments the index starting at 1 until a non-existent
        directory is found, then creates it.

        Returns:
            Directory referencing the created path (in macro form if inside project).

        Raises:
            DirectoryError: If the directory cannot be created.
        """
        match self._existing_dir_policy:
            case os_events.ExistingFilePolicy.CREATE_NEW:
                return self._create_with_versioning()
            case os_events.ExistingFilePolicy.OVERWRITE:
                return self._create_direct()
            case os_events.ExistingFilePolicy.FAIL:
                resolved = pathlib.Path(self.resolve())
                if resolved.exists():
                    msg = f"Attempted to create directory. Failed because directory already exists: {resolved}"
                    raise DirectoryError(msg)
                return self._create_direct()
            case _:
                msg = f"Unsupported existing directory policy: {self._existing_dir_policy!r}"
                raise DirectoryError(msg)

    def _create_with_versioning(self) -> Directory:
        """Use GetNextVersionIndexRequest to find an available version slot, then create it.

        If the path is a MacroPath with an ``_index`` variable, we can use it directly, if it's a string, we just add index in the end.
        """
        if isinstance(self._dir_path, project_events.MacroPath):
            macro_path = self._dir_path
        else:
            try:
                parsed = macro_parser.ParsedMacro(self._dir_path)
                has_variables = bool(parsed.get_variables())
            except macro_parser.MacroSyntaxError as exc:
                msg = f"Attempted to create versioned directory. Failed because path is not a valid macro: {self._dir_path}"
                raise DirectoryError(msg) from exc

            if has_variables:
                macro_path = project_events.MacroPath(parsed, {})
            else:
                macro_path = project_events.MacroPath(macro_parser.ParsedMacro(self._dir_path + "_{_index}"), {})

        # Get the next available version index for this macro path.
        # The macro is expected to contain an {_index} variable, which is used to find the next available version.
        index_result = griptape_nodes_mod.GriptapeNodes.handle_request(
            os_events.GetNextVersionIndexRequest(macro_path=macro_path)
        )
        if not isinstance(index_result, os_events.GetNextVersionIndexResultSuccess):
            msg = f"Attempted to create versioned directory. Failed to find available version index: {index_result.result_details}"
            raise DirectoryError(msg)

        index = index_result.index if index_result.index is not None else 1
        variables = macro_path.variables | {"_index": index}
        resolve_result = griptape_nodes_mod.GriptapeNodes.handle_request(
            project_events.GetPathForMacroRequest(parsed_macro=macro_path.parsed_macro, variables=variables)
        )
        if not isinstance(resolve_result, project_events.GetPathForMacroResultSuccess):
            msg = f"Attempted to create versioned directory. Failed to resolve macro: {resolve_result.result_details}"
            raise DirectoryError(msg)

        absolute_path = resolve_result.absolute_path
        mkdir_result = griptape_nodes_mod.GriptapeNodes.handle_request(
            os_events.MakeDirectoryRequest(path=str(absolute_path), create_parents=self._create_parents, exist_ok=False)
        )
        if not isinstance(mkdir_result, os_events.MakeDirectoryResultSuccess):
            msg = f"Attempted to create versioned directory. Failed to create '{absolute_path}': {mkdir_result.result_details}"
            raise DirectoryError(msg)

        locked_macro = project_events.MacroPath(macro_path.parsed_macro, variables)
        return _map_to_macro_directory(absolute_path, locked_macro)

    def _create_direct(self) -> Directory:
        """Create the directory without versioning."""
        resolved = pathlib.Path(_resolve_dir_path(self._dir_path))
        mkdir_result = griptape_nodes_mod.GriptapeNodes.handle_request(
            os_events.MakeDirectoryRequest(path=str(resolved), create_parents=self._create_parents, exist_ok=True)
        )
        if not isinstance(mkdir_result, os_events.MakeDirectoryResultSuccess):
            msg = f"Attempted to create directory. Failed to create '{resolved}': {mkdir_result.result_details}"
            raise DirectoryError(msg)

        return _map_to_macro_directory(resolved, self._dir_path)


def _resolve_dir_path(dir_path: str | project_events.MacroPath) -> str:
    """Resolve a directory path, handling MacroPath resolution if needed.

    Args:
        dir_path: A plain path string or a MacroPath.

    Returns:
        A resolved path string.

    Raises:
        DirectoryError: If macro resolution fails.
    """
    if isinstance(dir_path, str):
        try:
            parsed = macro_parser.ParsedMacro(dir_path)
        except macro_parser.MacroSyntaxError as exc:
            msg = f"Attempted to resolve directory path. Failed because path has invalid macro syntax: {dir_path!r}"
            raise DirectoryError(msg) from exc
        if not parsed.get_variables():
            return dir_path
        macro_path = project_events.MacroPath(parsed, {})
    else:
        macro_path = dir_path

    resolve_result = griptape_nodes_mod.GriptapeNodes.handle_request(
        project_events.GetPathForMacroRequest(parsed_macro=macro_path.parsed_macro, variables=macro_path.variables)
    )
    if not isinstance(resolve_result, project_events.GetPathForMacroResultSuccess):
        msg = f"Failed to resolve macro path '{macro_path.parsed_macro.template}': {resolve_result.result_details}"
        raise DirectoryError(msg)
    return str(resolve_result.absolute_path)



def _map_to_macro_directory(absolute_path: pathlib.Path, fallback_path: str | project_events.MacroPath) -> Directory:
    """Attempt to map the created directory path to a portable macro form.

    Returns a Directory holding the macro template when the path is inside
    a project directory, so callers can store a portable reference.
    Falls back to the locked MacroPath or absolute path string if mapping fails.
    """
    mapped = project_file._attempt_map_to_project(absolute_path)
    if mapped is not None:
        return Directory(mapped)
    if isinstance(fallback_path, project_events.MacroPath):
        return Directory(fallback_path)
    return Directory(str(absolute_path))
