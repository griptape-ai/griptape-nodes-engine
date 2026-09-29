"""Hooks for running the engine inside an environment another tool has prepared.

A studio can start the engine inside an environment its own tools resolve (a package manager, a
container, a launcher). Three hooks let that environment, rather than the engine, decide what runs:

- `GTN_LIBRARY_PATHS` lists library manifests the environment provides. They register like
  `libraries_to_register` entries, ahead of them.
- `library.dependency_source = "environment"` stops the engine from building virtual
  environments or downloading libraries, and limits loading to the libraries the environment lists.
- `worker.command_prefix` puts words in front of each worker's command, so a worker can be started
  inside the environment its library needs. `{library_request}` is filled from
  `GTN_LIBRARY_WORKER_REQUESTS`.

Nothing here knows which tool prepared the environment.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.managers.settings import (
    LIBRARY_DEPENDENCY_SOURCE_KEY,
    LIBRARY_ENVIRONMENT_ALLOWS_SANDBOX_KEY,
    WORKER_COMMAND_PREFIX_KEY,
    LibraryDependencySource,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from griptape_nodes.retained_mode.managers.config_manager import ConfigManager

logger = logging.getLogger("griptape_nodes")

LIBRARY_PATHS_ENV_VAR = "GTN_LIBRARY_PATHS"
LIBRARY_WORKER_REQUESTS_ENV_VAR = "GTN_LIBRARY_WORKER_REQUESTS"

LIBRARY_REQUEST_PLACEHOLDER = "{library_request}"
LIBRARY_NAME_PLACEHOLDER = "{library_name}"
ENGINE_VERSION_PLACEHOLDER = "{engine_version}"

# Separates a library's name from its request inside one GTN_LIBRARY_WORKER_REQUESTS entry. The
# first one splits, so a request may itself contain it (`lib_foo==1.4.2`).
_WORKER_REQUEST_SEPARATOR = "="


@dataclass(frozen=True)
class WorkerCommand:
    """The full argument list to start a worker with."""

    args: list[str]


@dataclass(frozen=True)
class WorkerCommandRefusal:
    """Why a worker must not be started, phrased to follow "Library 'X' cannot run right now: "."""

    reason: str


def read_dependency_source(config_manager: ConfigManager) -> LibraryDependencySource:
    """The configured `library.dependency_source`, in any letter case.

    The merged config keeps the raw value a config file carries, so it is normalized here. A value
    that is not a known source reads as 'venv'; the Settings validator has already warned about it.
    """
    raw_value = config_manager.get_config_value(
        LIBRARY_DEPENDENCY_SOURCE_KEY, default=LibraryDependencySource.VENV.value
    )
    if isinstance(raw_value, LibraryDependencySource):
        return raw_value
    if not isinstance(raw_value, str):
        return LibraryDependencySource.VENV
    try:
        return LibraryDependencySource(raw_value.strip().lower())
    except ValueError:
        return LibraryDependencySource.VENV


def uses_environment_dependencies(config_manager: ConfigManager) -> bool:
    """Whether the environment, not the engine, provides libraries and their dependencies."""
    return read_dependency_source(config_manager) is LibraryDependencySource.ENVIRONMENT


def environment_allows_sandbox(config_manager: ConfigManager) -> bool:
    """The configured `library.environment_allows_sandbox`, read from its raw config value.

    Only a real true, or the text 'true' in any letter case, allows the sandbox; anything else
    keeps it refused. The Settings validator has already warned about a value it could not read.
    """
    raw_value = config_manager.get_config_value(LIBRARY_ENVIRONMENT_ALLOWS_SANDBOX_KEY, default=False)
    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, str):
        return raw_value.strip().lower() == "true"
    return False


def sandbox_refused_by_environment(config_manager: ConfigManager) -> bool:
    """Whether the environment provides the libraries and has not opted in to a sandbox library."""
    return uses_environment_dependencies(config_manager) and not environment_allows_sandbox(config_manager)


def read_worker_command_prefix(config_manager: ConfigManager) -> list[str]:
    """The configured `worker.command_prefix`, or an empty list when it is unset or unusable."""
    raw_value = config_manager.get_config_value(WORKER_COMMAND_PREFIX_KEY, default=[])
    if not isinstance(raw_value, list):
        return []
    if not all(isinstance(word, str) for word in raw_value):
        logger.warning(
            "Ignoring %s: every entry must be text, got %r. Workers start without a prefix.",
            WORKER_COMMAND_PREFIX_KEY,
            raw_value,
        )
        return []
    return list(raw_value)


def library_paths_from_environment(environ: Mapping[str, str]) -> list[str]:
    """The library manifest paths listed in `GTN_LIBRARY_PATHS`, in order, blanks and repeats dropped."""
    raw_value = environ.get(LIBRARY_PATHS_ENV_VAR, "")
    entries = [entry.strip() for entry in raw_value.split(os.pathsep)]
    return list(dict.fromkeys(entry for entry in entries if entry))


def worker_requests_from_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Each library's worker request from `GTN_LIBRARY_WORKER_REQUESTS`, keyed by library name.

    Entries are `<library name>=<request>`, separated by `os.pathsep`; the library name is the
    manifest's `name`. The first entry for a library wins, the same precedence a search path gives
    its earlier entries. An entry without a name or a request is skipped with a warning.
    """
    raw_value = environ.get(LIBRARY_WORKER_REQUESTS_ENV_VAR, "")
    requests: dict[str, str] = {}
    for raw_entry in raw_value.split(os.pathsep):
        entry = raw_entry.strip()
        if not entry:
            continue
        library_name, separator, request = entry.partition(_WORKER_REQUEST_SEPARATOR)
        library_name = library_name.strip()
        request = request.strip()
        if not separator or not library_name or not request:
            logger.warning(
                "Ignoring entry %r in %s: expected '<library name>%s<request>'.",
                entry,
                LIBRARY_WORKER_REQUESTS_ENV_VAR,
                _WORKER_REQUEST_SEPARATOR,
            )
            continue
        if library_name in requests:
            if requests[library_name] != request:
                logger.warning(
                    "%s lists library '%s' more than once; using '%s' and ignoring '%s'.",
                    LIBRARY_WORKER_REQUESTS_ENV_VAR,
                    library_name,
                    requests[library_name],
                    request,
                )
            continue
        requests[library_name] = request
    return requests


def resolve_worker_command(  # noqa: PLR0913 (each input is a separate fact about this worker)
    *,
    command: list[str],
    prefix: list[str],
    library_name: str,
    worker_requests: Mapping[str, str],
    engine_version: str,
    environment_mode: bool,
) -> WorkerCommand | WorkerCommandRefusal:
    """Put the configured prefix in front of a worker's command, with its placeholders filled.

    A word that is exactly `{library_request}` becomes one word per space-separated part of the
    request, so one entry can name several things for the tool to resolve. Anywhere else a
    placeholder is replaced as text.

    When the prefix uses `{library_request}` and the library has no entry, the environment has not
    said how to run this library's worker. In environment mode that refuses the worker: the
    engine's own environment was never meant to run it, so starting it unprefixed would run the
    library against whatever packages happen to be there. In venv mode the library runs without the
    prefix, as it would with no prefix configured.

    Args:
        command: The worker command the prefix goes in front of.
        prefix: The configured `worker.command_prefix`.
        library_name: The library the worker serves (its manifest `name`).
        worker_requests: Worker requests by library name, from `GTN_LIBRARY_WORKER_REQUESTS`.
        engine_version: This engine's version, for `{engine_version}`.
        environment_mode: Whether `library.dependency_source` is 'environment'.
    """
    if not prefix:
        return WorkerCommand(args=list(command))

    needs_request = any(LIBRARY_REQUEST_PLACEHOLDER in word for word in prefix)
    request = worker_requests.get(library_name)

    if needs_request and request is None and environment_mode:
        return WorkerCommandRefusal(
            reason=(
                f"its worker process needs to know which packages to start with, and the environment "
                f"does not say (there is no '{library_name}' entry in {LIBRARY_WORKER_REQUESTS_ENV_VAR}), "
                f"so the worker was not started. Ask whoever set up this environment to add one."
            )
        )

    if needs_request and request is None:
        logger.info(
            "Starting the worker for library '%s' without %s: it has no entry in %s.",
            library_name,
            WORKER_COMMAND_PREFIX_KEY,
            LIBRARY_WORKER_REQUESTS_ENV_VAR,
        )
        return WorkerCommand(args=list(command))

    request_text = request or ""
    expanded: list[str] = []
    for word in prefix:
        if word == LIBRARY_REQUEST_PLACEHOLDER:
            expanded.extend(request_text.split())
            continue
        # The request goes in last, so text inside it is never mistaken for a placeholder.
        filled = word.replace(LIBRARY_NAME_PLACEHOLDER, library_name)
        filled = filled.replace(ENGINE_VERSION_PLACEHOLDER, engine_version)
        filled = filled.replace(LIBRARY_REQUEST_PLACEHOLDER, request_text)
        expanded.append(filled)
    return WorkerCommand(args=[*expanded, *command])
