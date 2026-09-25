"""Read image metadata and copy/paste payloads that earlier engines wrote as pickle.

This module is the engine's only use of pickle, and every workaround pickle-era data needs lives
here. Delete it, and the fallbacks that call it, to stop reading that data.

Unpickling builds only classes the value codec can build, and imports only modules that are
already loaded or belong to griptape or griptape_nodes, so a payload cannot call arbitrary
functions or import arbitrary modules.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import io
import logging
import pickle
import sys
from typing import TYPE_CHECKING, Any

from griptape_nodes.retained_mode.events.flow_events import SerializedFlowCommands
from griptape_nodes.retained_mode.events.node_events import SerializedSelectedNodesCommands
from griptape_nodes.serialization.type_names import DYNAMIC_MODULE_PREFIX, is_dynamic_module_name
from griptape_nodes.serialization.values import JsonValue, ValueEncodeError, encode_value, has_plain_data_form

if TYPE_CHECKING:
    from collections.abc import Collection

logger = logging.getLogger("griptape_nodes")

# Packages whose modules a payload may cause to be imported. Anything else must already be loaded.
_IMPORTABLE_PACKAGES = frozenset({"griptape", "griptape_nodes"})

# Classes pickle needs to rebuild values the codec encodes by other means.
_PICKLE_BUILDING_BLOCKS: frozenset[type] = frozenset({datetime.timezone})


class LegacyPickleError(Exception):
    """Data from an earlier engine could not be read. The message completes 'Failed because ...'."""


def load_legacy_pickle(data: bytes, library_modules: Collection[str]) -> Any:
    """Unpickle ``data``, building only classes the value codec can build.

    Args:
        data: The pickle.
        library_modules: The stable module name of every registered library file, used to find
            library classes pickled under another process's module name.

    Raises:
        LegacyPickleError: The data is not a pickle, or names something this reader refuses.
    """
    try:
        return _RestrictedUnpickler(io.BytesIO(data), library_modules).load()
    except LegacyPickleError:
        raise
    except Exception as error:
        # A malformed pickle, or a constructor it calls, can fail in many ways; each means the same here.
        msg = f"the data is not readable: {error}"
        raise LegacyPickleError(msg) from error


def read_legacy_image_flow_commands(text: str, library_modules: Collection[str]) -> SerializedFlowCommands:
    """Read flow commands that earlier engines embedded in images as base64-encoded pickle.

    Raises:
        LegacyPickleError: The text is not such a payload.
    """
    try:
        data = base64.b64decode(text, validate=True)
    except binascii.Error as error:
        msg = f"the data is neither JSON nor base64: {error}"
        raise LegacyPickleError(msg) from error
    commands = load_legacy_pickle(data, library_modules)
    if not isinstance(commands, SerializedFlowCommands):
        msg = f"the data holds a '{type(commands).__qualname__}', not flow commands"
        raise LegacyPickleError(msg)
    _encode_value_pools(commands)
    logger.warning(
        "Loaded a workflow from an image saved by an earlier version of Griptape Nodes. A later release "
        "will stop reading images saved that way. Save the image again to keep its workflow loadable."
    )
    return commands


def read_legacy_clipboard_commands(text: str, library_modules: Collection[str]) -> SerializedSelectedNodesCommands:
    """Read node commands that earlier engines put on the clipboard as a latin-1 pickle string.

    Raises:
        LegacyPickleError: The text is not such a payload.
    """
    commands = load_legacy_pickle(_latin1_bytes(text), library_modules)
    if not isinstance(commands, SerializedSelectedNodesCommands):
        msg = f"the data holds a '{type(commands).__qualname__}', not copied nodes"
        raise LegacyPickleError(msg)
    logger.warning(
        "Pasted nodes copied by an earlier version of Griptape Nodes. A later release will stop reading "
        "nodes copied that way. Copy them again to paste them in later releases."
    )
    return commands


def read_legacy_clipboard_value(text: str, library_modules: Collection[str]) -> JsonValue:
    """Read a parameter value that earlier engines put on the clipboard as a latin-1 pickle string.

    Returns the value encoded, the form copied values take today.

    Raises:
        LegacyPickleError: The text is not such a payload.
    """
    return _encode_legacy_value(load_legacy_pickle(_latin1_bytes(text), library_modules))


class _RestrictedUnpickler(pickle.Unpickler):
    def __init__(self, file: io.BytesIO, library_modules: Collection[str]) -> None:
        super().__init__(file)
        self._library_modules = library_modules

    def find_class(self, module: str, name: str) -> Any:
        if is_dynamic_module_name(module) and module not in sys.modules:
            module = self._library_module_for(module, name)
        if module not in sys.modules and module.partition(".")[0] not in _IMPORTABLE_PACKAGES:
            msg = f"the data refers to '{module}.{name}', whose module is not loaded"
            raise LegacyPickleError(msg)
        resolved = super().find_class(module, name)
        if not isinstance(resolved, type):
            msg = f"the data refers to '{module}.{name}', which is not a class"
            raise LegacyPickleError(msg)
        if resolved not in _PICKLE_BUILDING_BLOCKS and not has_plain_data_form(resolved):
            msg = f"the data refers to '{module}.{name}', which is not a type of saved value"
            raise LegacyPickleError(msg)
        return resolved

    def _library_module_for(self, dynamic_module: str, name: str) -> str:
        """Find the library file another process pickled ``name`` from, under its own module name.

        Library files load as ``gtn_dynamic_module_<file name>_<hash>``, and the hash is Python's
        per-process ``hash()`` of the file's path, so it cannot be recomputed. Match the file name
        against registered library files instead, and give up if more than one has that name.
        """
        file_name = dynamic_module.removeprefix(DYNAMIC_MODULE_PREFIX).rpartition("_")[0]
        stem = file_name.removesuffix("_py").replace("-", "_")
        matches = [module for module in self._library_modules if module.rpartition(".")[2] == stem]
        if not matches:
            msg = f"the data refers to '{name}' from a library file named '{stem}', which no registered library has"
            raise LegacyPickleError(msg)
        if len(matches) > 1:
            msg = (
                f"the data refers to '{name}' from a library file named '{stem}', which more than one "
                "registered library has"
            )
            raise LegacyPickleError(msg)
        return matches[0]


def _encode_value_pools(commands: SerializedFlowCommands) -> None:
    """Encode the pooled values of ``commands`` and its sub-flows, which earlier engines kept live."""
    commands.unique_parameter_uuid_to_values = {
        key: _encode_legacy_value(value) for key, value in commands.unique_parameter_uuid_to_values.items()
    }
    for sub_flow_commands in commands.sub_flows_commands:
        _encode_value_pools(sub_flow_commands)


def _encode_legacy_value(value: Any) -> JsonValue:
    try:
        return encode_value(value)
    except ValueEncodeError as error:
        msg = f"a saved value has no plain-data form: {error}"
        raise LegacyPickleError(msg) from error


def _latin1_bytes(text: str) -> bytes:
    try:
        return text.encode("latin-1")
    except UnicodeEncodeError as error:
        msg = "the data is not a pickle string"
        raise LegacyPickleError(msg) from error
