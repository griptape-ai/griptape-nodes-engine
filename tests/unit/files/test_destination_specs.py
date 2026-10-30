"""Destinations survive the trip to another process as plain data, keeping every setting."""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from griptape_nodes.common.macro_parser import ParsedMacro
from griptape_nodes.files.directory import DirectoryDestination
from griptape_nodes.files.file import FileDestination
from griptape_nodes.files.file_sequence import FileSequenceDestination
from griptape_nodes.files.project_file import ProjectFileDestination, file_destination_from_spec
from griptape_nodes.retained_mode.events.destination_events import (
    DirectoryDestinationSpec,
    FileDestinationSpec,
    FileSequenceDestinationSpec,
    GetConnectedDirectoryDestinationResultSuccess,
    GetConnectedFileDestinationResultSuccess,
    GetConnectedFileSequenceDestinationResultSuccess,
)
from griptape_nodes.retained_mode.events.os_events import ExistingFilePolicy
from griptape_nodes.retained_mode.events.project_events import MacroPath
from griptape_nodes.retained_mode.file_metadata.sidecar_metadata import SidecarContent, SituationMetadata
from tests.simulated_worker import through_the_wire

_MACRO = MacroPath(
    ParsedMacro("{outputs}/{sub_dirs?:/}{file_name_base}.{file_extension}"),
    {"file_name_base": "render", "file_extension": "png"},
)
_LOCKED_SEQUENCE_MACRO = MacroPath(ParsedMacro("{outputs}/render_v{_index:03}/render.####.exr"), {"_index": 3})
_METADATA = SidecarContent(
    situation=SituationMetadata(name="save_node_output", macro="{outputs}/x", variables={"a": "b"})
)

_FILE_SPECS = [
    FileDestinationSpec(
        file_path="/renders/out.png",
        existing_file_policy=ExistingFilePolicy.FAIL,
        append=True,
        create_parents=False,
        coerce_extension_to_match_bytes=False,
        file_metadata=None,
        map_to_project=False,
    ),
    FileDestinationSpec(
        file_path=_MACRO,
        existing_file_policy=ExistingFilePolicy.CREATE_NEW,
        append=False,
        create_parents=True,
        coerce_extension_to_match_bytes=True,
        file_metadata=_METADATA,
        map_to_project=True,
    ),
]


def _init_argument_names(cls: type) -> set[str]:
    return {name for name in inspect.signature(cls.__init__).parameters if name != "self"}


def _field_names(spec_type: type) -> set[str]:
    return {spec_field.name for spec_field in dataclasses.fields(spec_type)}


class TestSpecsCrossTheWire:
    @pytest.mark.parametrize("spec", _FILE_SPECS, ids=["plain_path", "macro_path"])
    def test_file_spec(self, spec: FileDestinationSpec) -> None:
        result = GetConnectedFileDestinationResultSuccess(destination=spec, result_details="ok")

        assert through_the_wire(result).destination == spec

    @pytest.mark.parametrize("dir_path", ["/renders/out", _MACRO], ids=["plain_path", "macro_path"])
    def test_directory_spec(self, dir_path: str | MacroPath) -> None:
        spec = DirectoryDestinationSpec(
            dir_path=dir_path, existing_dir_policy=ExistingFilePolicy.OVERWRITE, create_parents=False
        )
        result = GetConnectedDirectoryDestinationResultSuccess(destination=spec, result_details="ok")

        assert through_the_wire(result).destination == spec

    def test_file_sequence_spec_keeps_its_locked_version(self) -> None:
        spec = FileSequenceDestinationSpec(
            macro_path=_LOCKED_SEQUENCE_MACRO, existing_file_policy=ExistingFilePolicy.FAIL, create_parents=False
        )
        result = GetConnectedFileSequenceDestinationResultSuccess(destination=spec, result_details="ok")

        assert through_the_wire(result).destination == spec

    def test_no_destination(self) -> None:
        result = GetConnectedFileDestinationResultSuccess(destination=None, result_details="ok")

        assert through_the_wire(result).destination is None


class TestRebuildingKeepsEverySetting:
    @pytest.mark.parametrize("spec", _FILE_SPECS, ids=["plain_path", "macro_path"])
    def test_file_destination(self, spec: FileDestinationSpec) -> None:
        assert file_destination_from_spec(spec).to_spec() == spec

    @pytest.mark.parametrize(
        ("map_to_project", "expected_type"), [(False, FileDestination), (True, ProjectFileDestination)]
    )
    def test_file_destination_keeps_its_project_mapping(self, *, map_to_project: bool, expected_type: type) -> None:
        spec = dataclasses.replace(_FILE_SPECS[0], map_to_project=map_to_project)

        assert type(file_destination_from_spec(spec)) is expected_type

    def test_directory_destination(self) -> None:
        destination = DirectoryDestination(_MACRO, existing_dir_policy=ExistingFilePolicy.FAIL, create_parents=False)

        assert DirectoryDestination.from_spec(destination.to_spec()).to_spec() == destination.to_spec()

    def test_file_sequence_destination(self) -> None:
        destination = FileSequenceDestination(
            _LOCKED_SEQUENCE_MACRO, existing_file_policy=ExistingFilePolicy.FAIL, create_parents=False
        )

        assert FileSequenceDestination.from_spec(destination.to_spec()).to_spec() == destination.to_spec()


class TestSpecsNameEveryConstructorArgument:
    """A setting added to a destination's constructor but not its spec would be dropped on the way to a worker."""

    def test_file_destination(self) -> None:
        assert _init_argument_names(FileDestination) == _field_names(FileDestinationSpec) - {"map_to_project"}

    def test_directory_destination(self) -> None:
        assert _init_argument_names(DirectoryDestination) == _field_names(DirectoryDestinationSpec)

    def test_file_sequence_destination(self) -> None:
        assert _init_argument_names(FileSequenceDestination) == _field_names(FileSequenceDestinationSpec)
