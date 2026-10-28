# ruff: noqa: PLR2004

"""Tests for running a node's declared pre-run validators at its turn."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from griptape_nodes.exe_types import input_checks
from griptape_nodes.exe_types.core_types import Parameter, ParameterList, ParameterMode
from griptape_nodes.exe_types.input_checks import (
    ImageInfo,
    VideoProbe,
    input_check_readings,
    local_image_info,
    local_video_probe,
)
from griptape_nodes.exe_types.local_objects import make_reference
from griptape_nodes.exe_types.node_error import NodeError
from griptape_nodes.exe_types.node_groups.subflow_node_group import SubflowNodeGroup
from griptape_nodes.exe_types.param_types.parameter_audio import ParameterAudio
from griptape_nodes.exe_types.param_types.parameter_bool import ParameterBool
from griptape_nodes.exe_types.param_types.parameter_button import ParameterButton
from griptape_nodes.exe_types.param_types.parameter_dict import ParameterDict
from griptape_nodes.exe_types.param_types.parameter_float import ParameterFloat
from griptape_nodes.exe_types.param_types.parameter_html import ParameterHtml
from griptape_nodes.exe_types.param_types.parameter_image import ParameterImage
from griptape_nodes.exe_types.param_types.parameter_int import ParameterInt
from griptape_nodes.exe_types.param_types.parameter_json import ParameterJson
from griptape_nodes.exe_types.param_types.parameter_python import ParameterPython
from griptape_nodes.exe_types.param_types.parameter_range import ParameterRange
from griptape_nodes.exe_types.param_types.parameter_string import ParameterString
from griptape_nodes.exe_types.param_types.parameter_video import ParameterVideo
from griptape_nodes.exe_types.param_types.parameter_xml import ParameterXml
from griptape_nodes.exe_types.param_types.parameter_yaml import ParameterYaml
from griptape_nodes.exe_types.pre_run_validation import (
    UncheckedInput,
    run_pre_run_validators,
    unchecked_inputs_message,
    unchecked_locations_by_name,
)
from griptape_nodes.exe_types.validators.base import MediaInfo, MediaNeed, NodeValidator, PreRunValidator
from griptape_nodes.exe_types.validators.common import ByParameter, MaxCount
from griptape_nodes.exe_types.validators.media import (
    AllowedFormats,
    AspectRatioRange,
    AudioCodecs,
    Duration,
    FrameRate,
    MaxFileSize,
    SideRange,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.parameter_events import (
    AddParameterToNodeRequest,
    AddParameterToNodeResultSuccess,
)
from tests.unit.exe_types.mocks import MockNode
from tests.unit.exe_types.validators.media_files import image_file

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from griptape_nodes.exe_types.node_types import BaseNode
    from griptape_nodes.retained_mode.engine import Engine

DOCS = "https://example.com/limits"


def _messages(errors: Sequence[Exception]) -> list[str]:
    return [str(error) for error in errors]


def _node_with(*parameters: Parameter) -> MockNode:
    node = MockNode(name="node")
    for parameter in parameters:
        node.add_parameter(parameter)
    return node


class _LimitedNode(MockNode):
    def input_limits_url(self) -> str | None:
        return DOCS


class TestSingleInputs:
    def test_reports_nothing_for_a_node_without_validators(self, tmp_path: Path) -> None:
        node = _node_with(ParameterImage(name="image"))
        node.set_parameter_value("image", str(image_file(tmp_path, 10, 10)))
        assert run_pre_run_validators(node) == []

    def test_names_the_input_by_its_display_name_and_kind(self, tmp_path: Path) -> None:
        node = _node_with(
            ParameterImage(name="image", display_name="Start Frame", pre_run_validators=[SideRange(min=300)])
        )
        node.set_parameter_value("image", str(image_file(tmp_path, 200, 200)))
        assert _messages(run_pre_run_validators(node)) == [
            "The image in 'Start Frame' is 200x200 and each side must be at least 300 pixels. Use a larger one."
        ]

    def test_falls_back_to_the_parameter_name(self, tmp_path: Path) -> None:
        node = _node_with(ParameterImage(name="image_tail", pre_run_validators=[SideRange(min=300)]))
        node.set_parameter_value("image_tail", str(image_file(tmp_path, 200, 200)))
        (message,) = _messages(run_pre_run_validators(node))
        assert message.startswith("The image in 'image_tail' is 200x200")

    def test_names_audio_as_an_audio_clip(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        clip = tmp_path / "clip.mp3"
        clip.write_bytes(b"audio")
        probe = input_checks.AudioProbe(mime_type="audio/mpeg", codec="mp3", profile=None, seconds=9.0)
        monkeypatch.setattr(input_checks, "_probe_audio", lambda _location: probe)
        node = _node_with(ParameterAudio(name="voice", pre_run_validators=[Duration(max=5)]))
        node.set_parameter_value("voice", str(clip))
        assert _messages(run_pre_run_validators(node)) == [
            "The audio clip in 'voice' is 9.00 seconds long and the limit is 5. Trim it in an audio editor."
        ]

    def test_links_the_nodes_input_limits_page(self, tmp_path: Path) -> None:
        node = _LimitedNode(name="node")
        node.add_parameter(ParameterImage(name="image", pre_run_validators=[SideRange(min=300)]))
        node.set_parameter_value("image", str(image_file(tmp_path, 200, 200)))
        (error,) = run_pre_run_validators(node)
        assert isinstance(error, NodeError)
        assert error.links
        assert error.links[0].url == DOCS

    def test_skips_an_empty_input(self) -> None:
        node = _node_with(ParameterImage(name="image", pre_run_validators=[SideRange(min=300)]))
        assert run_pre_run_validators(node) == []

    def test_collects_every_error_from_every_parameter(self, tmp_path: Path) -> None:
        node = _node_with(
            ParameterImage(name="first", pre_run_validators=[SideRange(min=300), AspectRatioRange("1:1", "1:1")]),
            ParameterImage(name="second", pre_run_validators=[AllowedFormats("image/jpeg")]),
        )
        node.set_parameter_value("first", str(image_file(tmp_path, 200, 100)))
        node.set_parameter_value("second", str(image_file(tmp_path, 400, 400)))
        assert _messages(run_pre_run_validators(node)) == [
            "The image in 'first' is 200x100 and each side must be at least 300 pixels. Use a larger one.",
            "The image in 'first' is 200x100, which is too wide. The aspect ratio must be between 1:1 and 1:1. Crop it.",
            "The image in 'second' is in PNG format. Convert it to JPEG.",
        ]

    def test_runs_a_custom_validator_on_a_string_parameter(self) -> None:
        @dataclass(frozen=True)
        class NotEmpty(PreRunValidator):
            def check(self, value: Any, *, subject: str, media: MediaInfo, docs_url: str | None) -> NodeError | None:  # noqa: ARG002
                if value.strip():
                    return None
                return NodeError(f"{subject} is blank. Write a prompt.")

        node = _node_with(ParameterString(name="prompt", pre_run_validators=[NotEmpty()]))
        node.set_parameter_value("prompt", "   ")
        assert _messages(run_pre_run_validators(node)) == ["The input in 'prompt' is blank. Write a prompt."]


class TestReadOnce:
    """The engine reads each value once per parameter, for every reading its validators need together."""

    @pytest.fixture
    def video_reads(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        reads: list[str] = []

        def counting_probe(location: str) -> VideoProbe:
            reads.append(location)
            return VideoProbe(info=ImageInfo(4000, 1000, None), seconds=40.0, fps=24.0, mime_type="video/mp4")

        monkeypatch.setattr(input_checks, "_probe_video", counting_probe)
        return reads

    def test_probes_a_video_once_for_every_validator_on_it(self, tmp_path: Path, video_reads: list[str]) -> None:
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"video")
        node = _node_with(
            ParameterVideo(
                name="video",
                pre_run_validators=[
                    AllowedFormats("video/mp4"),
                    SideRange(max=2048),
                    AspectRatioRange("1:3", "3:1"),
                    Duration(max=30),
                    FrameRate(max=60),
                    MaxFileSize(1024),
                ],
            )
        )
        node.set_parameter_value("video", str(clip))
        with input_check_readings():
            messages = _messages(run_pre_run_validators(node))
        assert messages == [
            "The video in 'video' is 4000x1000 and each side must be at most 2048 pixels. Resize it smaller.",
            "The video in 'video' is 4000x1000, which is too wide. The aspect ratio must be between 1:3 and 3:1. Crop it.",
            "The video in 'video' is 40.00 seconds long and the limit is 30. Use a Trim Video node to shorten it.",
        ]
        assert video_reads == [str(clip)]

    def test_reads_without_probing_when_only_size_is_needed(self, tmp_path: Path, video_reads: list[str]) -> None:
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"video")
        node = _node_with(ParameterVideo(name="video", pre_run_validators=[MaxFileSize(1)]))
        node.set_parameter_value("video", str(clip))
        assert len(run_pre_run_validators(node)) == 1
        assert video_reads == []

    def test_a_node_validator_and_the_nodes_own_checks_reuse_the_reading(
        self, tmp_path: Path, video_reads: list[str]
    ) -> None:
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"video")
        seen: list[MediaInfo] = []

        class Reads(NodeValidator):
            def check(
                self,
                node: BaseNode,
                *,
                media: Mapping[str, Sequence[MediaInfo]],
                docs_url: str | None,  # noqa: ARG002
            ) -> list[NodeError]:
                seen.extend(media["video"])
                # A custom check that calls a reader itself gets the same reading.
                local_video_probe(node.get_parameter_value("video"))
                return []

        class ReadingNode(MockNode):
            node_validators: ClassVar[list[NodeValidator]] = [Reads()]

        node = ReadingNode(name="node")
        node.add_parameter(ParameterVideo(name="video", pre_run_validators=[Duration(max=30)]))
        node.set_parameter_value("video", str(clip))
        with input_check_readings():
            run_pre_run_validators(node)
            local_video_probe(str(clip))
        assert video_reads == [str(clip)]
        assert len(seen) == 1
        assert seen[0].video is not None
        assert seen[0].video.seconds == 40.0

    def test_reads_again_in_the_next_pass(self, tmp_path: Path, video_reads: list[str]) -> None:
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"video")
        with input_check_readings():
            local_video_probe(str(clip))
            local_video_probe(str(clip))
        with input_check_readings():
            local_video_probe(str(clip))
        assert video_reads == [str(clip), str(clip)]

    def test_reads_each_time_outside_a_pass(self, tmp_path: Path, video_reads: list[str]) -> None:
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"video")
        local_video_probe(str(clip))
        local_video_probe(str(clip))
        assert video_reads == [str(clip), str(clip)]

    def test_remembers_an_unreadable_input(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        reads: list[str] = []

        def unreadable(location: str) -> None:
            reads.append(location)

        monkeypatch.setattr(input_checks, "_read_image_info", unreadable)
        path = str(tmp_path / "broken.png")
        with input_check_readings():
            assert local_image_info(path) is None
            assert local_image_info(path) is None
        assert reads == [path]

    def test_a_nested_pass_shares_the_outer_readings(self, tmp_path: Path, video_reads: list[str]) -> None:
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"video")
        with input_check_readings():
            local_video_probe(str(clip))
            with input_check_readings():
                local_video_probe(str(clip))
            local_video_probe(str(clip))
        assert video_reads == [str(clip)]


class TestLists:
    @staticmethod
    def _list_node(*validators: PreRunValidator) -> MockNode:
        return _node_with(
            ParameterList(
                name="reference_images",
                tooltip="",
                input_types=["ImageUrlArtifact"],
                type="ImageUrlArtifact",
                display_name="reference images",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                pre_run_validators=validators,
            )
        )

    def test_checks_each_item_by_its_position(self, tmp_path: Path) -> None:
        node = self._list_node(SideRange(min=300))
        images = [str(image_file(tmp_path, 400, 400)), str(image_file(tmp_path, 200, 1000))]
        node.set_parameter_value("reference_images", images)
        assert _messages(run_pre_run_validators(node)) == [
            "Image 2 in 'reference images' is 200x1000 and each side must be at least 300 pixels. Use a larger one."
        ]

    def test_checks_items_set_row_by_row(self, tmp_path: Path) -> None:
        node = self._list_node(SideRange(min=300))
        parameter = node.get_parameter_by_name("reference_images")
        assert isinstance(parameter, ParameterList)
        for size in (100, 400, 200):
            row = parameter.add_child_parameter()
            node.set_parameter_value(row.name, str(image_file(tmp_path, size, size)))
        messages = _messages(run_pre_run_validators(node))
        assert [message.split(" is ")[0] for message in messages] == [
            "Image 1 in 'reference images'",
            "Image 3 in 'reference images'",
        ]

    def test_counts_the_list_with_max_count(self, tmp_path: Path) -> None:
        node = self._list_node(MaxCount(2, noun="images"), SideRange(min=1))
        images = [str(image_file(tmp_path, 10 + i, 10)) for i in range(4)]
        node.set_parameter_value("reference_images", images)
        assert _messages(run_pre_run_validators(node)) == [
            "4 images are connected to 'reference images' and the limit is 2. Remove 2."
        ]

    def test_reads_each_item_once(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        reads: list[str] = []

        def counting(location: str) -> ImageInfo:
            reads.append(location)
            return ImageInfo(100, 100, "image/png")

        monkeypatch.setattr(input_checks, "_read_image_info", counting)
        node = self._list_node(SideRange(min=300), AllowedFormats("image/png"), AspectRatioRange("1:2", "2:1"))
        images = [str(tmp_path / "a.png"), str(tmp_path / "b.png")]
        node.set_parameter_value("reference_images", images)
        with input_check_readings():
            assert len(run_pre_run_validators(node)) == 2
        assert reads == images

    def test_rows_get_no_copy_of_the_lists_validators(self) -> None:
        node = self._list_node(SideRange(min=300))
        parameter = node.get_parameter_by_name("reference_images")
        assert isinstance(parameter, ParameterList)
        row = parameter.add_child_parameter()
        assert row.pre_run_validators == ()


class TestPlainListParameters:
    """A list parameter that isn't a ParameterList holds its items in its own value."""

    @staticmethod
    def _node(*validators: PreRunValidator) -> MockNode:
        return _node_with(
            Parameter(
                name="reference_images",
                type="list[ImageUrlArtifact]",
                display_name="reference images",
                pre_run_validators=validators,
            )
        )

    def test_checks_each_item_skipping_empty_rows(self, tmp_path: Path) -> None:
        node = self._node(SideRange(min=300), MaxCount(2, noun="images"))
        node.set_parameter_value(
            "reference_images", [str(image_file(tmp_path, 400, 400)), None, str(image_file(tmp_path, 200, 1000))]
        )
        assert _messages(run_pre_run_validators(node)) == [
            "Image 2 in 'reference images' is 200x1000 and each side must be at least 300 pixels. Use a larger one."
        ]

    @pytest.mark.parametrize("value", [None, "not a list"], ids=["unset", "not-a-list"])
    def test_checks_a_value_that_is_not_a_list_as_empty(self, value: Any) -> None:
        node = self._node(SideRange(min=300), _Records())
        _Records.seen.clear()
        node.set_parameter_value("reference_images", value)
        assert run_pre_run_validators(node) == []
        assert _Records.seen == []


class TestByParameter:
    @staticmethod
    def _node() -> MockNode:
        return _node_with(
            ParameterString(name="model", default_value="pro"),
            ParameterImage(
                name="image",
                pre_run_validators=[
                    AllowedFormats("image/png"),
                    ByParameter(
                        "model",
                        {"pro": [SideRange(min=300)], "lite": [SideRange(min=100), MaxFileSize(1)]},
                        default=[SideRange(min=1000)],
                    ),
                ],
            ),
        )

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("pro", ["each side must be at least 300 pixels"]),
            ("lite", ["MB and the limit is"]),
            ("other", ["each side must be at least 1000 pixels"]),
        ],
    )
    def test_picks_the_limits_for_the_other_parameters_current_value(
        self, tmp_path: Path, model: str, expected: list[str]
    ) -> None:
        node = self._node()
        node.set_parameter_value("model", model)
        node.set_parameter_value("image", str(image_file(tmp_path, 200, 200)))
        messages = _messages(run_pre_run_validators(node))
        assert len(messages) == len(expected)
        for message, fragment in zip(messages, expected, strict=True):
            assert fragment in message

    def test_refuses_a_nested_by_parameter(self) -> None:
        with pytest.raises(TypeError, match="holds another ByParameter"):
            ByParameter("model", {"pro": [ByParameter("size", {})]})

    def test_checks_nothing_when_the_pick_lists_no_validators(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A model that doesn't send the input lists no limits for it, and the input isn't read.
        node = _node_with(
            ParameterString(name="model", default_value="fast"),
            ParameterImage(name="last_frame", pre_run_validators=[ByParameter("model", {"pro": [SideRange(min=300)]})]),
        )
        node.set_parameter_value("last_frame", str(image_file(tmp_path, 10, 10)))
        reads: list[str] = []
        monkeypatch.setattr(input_checks, "_read_image_info", reads.append)
        with input_check_readings():
            assert run_pre_run_validators(node) == []
        assert reads == []

    def test_refuses_a_picked_validator_the_parameter_cannot_hold(self) -> None:
        with pytest.raises(TypeError, match="checks audio, and the parameter holds image"):
            ParameterImage(name="image", pre_run_validators=[ByParameter("model", {"pro": [AudioCodecs("mp3")]})])

    def test_survives_a_parameter_copy(self) -> None:
        parameter = ParameterImage(name="image", pre_run_validators=[ByParameter("model", {"pro": [SideRange(min=1)]})])
        assert parameter.copy().pre_run_validators == parameter.pre_run_validators


class TestNodeValidators:
    def test_runs_after_the_parameter_validators_with_what_they_read(self, tmp_path: Path) -> None:
        class SameSize(NodeValidator):
            def check(
                self,
                node: BaseNode,  # noqa: ARG002
                *,
                media: Mapping[str, Sequence[MediaInfo]],
                docs_url: str | None,  # noqa: ARG002
            ) -> list[NodeError]:
                (first,) = media["first"]
                (second,) = media["second"]
                if first.frame == second.frame:
                    return []
                return [NodeError("'first' and 'second' must be the same size.", links=None)]

        class PairNode(_LimitedNode):
            node_validators: ClassVar[list[NodeValidator]] = [SameSize()]

        node = PairNode(name="node")
        node.add_parameter(ParameterImage(name="first", pre_run_validators=[SideRange(min=300)]))
        node.add_parameter(ParameterImage(name="second", pre_run_validators=[SideRange(min=1)]))
        node.set_parameter_value("first", str(image_file(tmp_path, 200, 200)))
        node.set_parameter_value("second", str(image_file(tmp_path, 400, 400)))
        assert _messages(run_pre_run_validators(node)) == [
            "The image in 'first' is 200x200 and each side must be at least 300 pixels. Use a larger one.",
            "'first' and 'second' must be the same size.",
        ]

    def test_runs_on_a_node_with_no_parameter_validators(self) -> None:
        class Always(NodeValidator):
            def check(
                self,
                node: BaseNode,
                *,
                media: Mapping[str, Sequence[MediaInfo]],
                docs_url: str | None,
            ) -> list[NodeError]:
                assert media == {}
                return [NodeError(f"{node.name} refused: {docs_url}", links=None)]

        class RuleNode(_LimitedNode):
            node_validators: ClassVar[list[NodeValidator]] = [Always()]

        assert _messages(run_pre_run_validators(RuleNode(name="rule"))) == [f"rule refused: {DOCS}"]


class TestParameterTypes:
    @pytest.mark.parametrize(
        ("build", "match"),
        [
            (
                lambda: ParameterString(name="p", pre_run_validators=[SideRange(min=1)]),
                "isn't an image, video or audio",
            ),
            (lambda: ParameterAudio(name="p", pre_run_validators=[SideRange(min=1)]), "checks image or video"),
            (lambda: ParameterImage(name="p", pre_run_validators=[Duration(max=1)]), "checks audio or video"),
            (lambda: ParameterImage(name="p", pre_run_validators=[MaxCount(2)]), "checks a list"),
            (lambda: Parameter(name="p", type="int", pre_run_validators=[MaxFileSize(1)]), "isn't an image"),
        ],
        ids=["side-on-string", "side-on-audio", "duration-on-image", "count-on-single", "size-on-int"],
    )
    def test_refuses_a_validator_its_type_cannot_hold(self, build: Any, match: str) -> None:
        with pytest.raises(TypeError, match=match):
            build()

    def test_accepts_media_validators_on_their_kinds(self) -> None:
        ParameterImage(name="i", pre_run_validators=[AllowedFormats("image/png"), SideRange(min=1), MaxFileSize(1)])
        ParameterVideo(name="v", pre_run_validators=[AllowedFormats("video/mp4"), Duration(max=1), FrameRate(max=1)])
        ParameterAudio(name="a", pre_run_validators=[AllowedFormats("audio/mpeg"), Duration(max=1)])

    @pytest.mark.parametrize(
        "parameter_class",
        [
            ParameterAudio,
            ParameterBool,
            ParameterButton,
            ParameterDict,
            ParameterFloat,
            ParameterHtml,
            ParameterImage,
            ParameterInt,
            ParameterJson,
            ParameterPython,
            ParameterRange,
            ParameterString,
            ParameterVideo,
            ParameterXml,
            ParameterYaml,
        ],
    )
    def test_every_parameter_type_keeps_its_pre_run_validators(self, parameter_class: type[Parameter]) -> None:
        validator = _Records()
        parameter = parameter_class(name="p", pre_run_validators=[validator])
        assert list(parameter.pre_run_validators) == [validator]

    def test_a_3d_parameter_keeps_its_pre_run_validators(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Parameter3D imports its artifact from the standard library, which the engine's tests don't install.
        artifact_module = ModuleType("griptape_nodes_library.three_d.three_d_artifact")
        artifact_module.ThreeDUrlArtifact = type("ThreeDUrlArtifact", (), {})  # pyright: ignore[reportAttributeAccessIssue]
        for name in ("griptape_nodes_library", "griptape_nodes_library.three_d"):
            monkeypatch.setitem(sys.modules, name, ModuleType(name))
        monkeypatch.setitem(sys.modules, artifact_module.__name__, artifact_module)
        monkeypatch.delitem(sys.modules, "griptape_nodes.exe_types.param_types.parameter_three_d", raising=False)
        parameter_3d = importlib.import_module("griptape_nodes.exe_types.param_types.parameter_three_d").Parameter3D

        validator = _Records()
        parameter = parameter_3d(name="model", pre_run_validators=[validator])
        assert list(parameter.pre_run_validators) == [validator]

    def test_keeps_on_set_validators_as_they_were(self) -> None:
        def refuse_blank(_parameter: Parameter, value: Any) -> None:
            if value == "":
                msg = "blank"
                raise ValueError(msg)

        node = _node_with(
            ParameterString(name="prompt", validators=[refuse_blank], pre_run_validators=[_Records()]),
        )
        parameter = node.get_parameter_by_name("prompt")
        assert parameter is not None
        assert parameter.validators == [refuse_blank]
        with pytest.raises(ValueError, match="blank"):
            node.set_parameter_value("prompt", "")
        node.set_parameter_value("prompt", "a cat")
        # Setting a value runs no pre-run validator; only the node's turn does.
        assert _Records.seen == []
        run_pre_run_validators(node)
        assert _Records.seen == ["a cat"]


class TestUncheckedInputs:
    """An input that took too long to read is named, so the user can be told; the node still runs."""

    @pytest.fixture(autouse=True)
    def slow_ffprobe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def run(cmd: list[str], **kwargs: Any) -> None:
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

        monkeypatch.setattr(input_checks, "_installed_ffprobe", lambda: "/usr/bin/ffprobe")
        monkeypatch.setattr(input_checks.subprocess, "run", run)

    @staticmethod
    def _clip(tmp_path: Path, name: str = "clip.mp4") -> str:
        clip = tmp_path / name
        clip.write_bytes(b"video")
        return str(clip)

    @staticmethod
    def _unchecked(node: BaseNode) -> tuple[list[NodeError], list[UncheckedInput]]:
        unchecked: list[UncheckedInput] = []
        with input_check_readings():
            errors = run_pre_run_validators(node, unchecked=unchecked)
        return errors, unchecked

    def test_names_a_video_that_took_too_long(self, tmp_path: Path) -> None:
        clip = self._clip(tmp_path)
        node = _node_with(ParameterVideo(name="video", display_name="Video", pre_run_validators=[Duration(max=5)]))
        node.set_parameter_value("video", clip)
        assert self._unchecked(node) == ([], [(clip, "The video in 'Video'")])

    def test_still_reports_what_it_could_read(self, tmp_path: Path) -> None:
        clip = self._clip(tmp_path)
        node = _node_with(ParameterVideo(name="video", pre_run_validators=[MaxFileSize(1), Duration(max=5)]))
        node.set_parameter_value("video", clip)
        errors, unchecked = self._unchecked(node)
        (message,) = _messages(errors)
        assert message.startswith("The video in 'video' is")
        assert unchecked == [(clip, "The video in 'video'")]

    def test_names_each_list_item_by_its_position(self, tmp_path: Path) -> None:
        first, second = self._clip(tmp_path, "a.mp4"), self._clip(tmp_path, "b.mp4")
        node = _node_with(
            ParameterList(
                name="clips",
                tooltip="",
                input_types=["VideoUrlArtifact"],
                type="VideoUrlArtifact",
                display_name="Reference Videos",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                pre_run_validators=[Duration(max=5)],
            )
        )
        node.set_parameter_value("clips", [first, second])
        assert self._unchecked(node)[1] == [
            (first, "Video 1 in 'Reference Videos'"),
            (second, "Video 2 in 'Reference Videos'"),
        ]

    def test_names_both_inputs_that_hold_the_same_file(self, tmp_path: Path) -> None:
        clip = self._clip(tmp_path)
        node = _node_with(
            ParameterVideo(name="start", display_name="Start", pre_run_validators=[Duration(max=5)]),
            ParameterVideo(name="end", display_name="End", pre_run_validators=[Duration(max=5)]),
        )
        node.set_parameter_value("start", clip)
        node.set_parameter_value("end", clip)
        assert self._unchecked(node)[1] == [(clip, "The video in 'Start'"), (clip, "The video in 'End'")]

    def test_does_not_name_an_input_sharing_the_file_whose_checks_all_ran(self, tmp_path: Path) -> None:
        clip = self._clip(tmp_path)
        node = _node_with(
            ParameterVideo(name="start", display_name="Start", pre_run_validators=[Duration(max=5)]),
            ParameterVideo(name="end", display_name="End", pre_run_validators=[MaxFileSize(10_000)]),
        )
        node.set_parameter_value("start", clip)
        node.set_parameter_value("end", clip)
        assert self._unchecked(node)[1] == [(clip, "The video in 'Start'")]

    def test_names_nothing_when_only_the_size_is_read(self, tmp_path: Path) -> None:
        node = _node_with(ParameterVideo(name="video", pre_run_validators=[MaxFileSize(10_000)]))
        node.set_parameter_value("video", self._clip(tmp_path))
        assert self._unchecked(node) == ([], [])

    def test_names_nothing_for_an_input_read_in_time(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            input_checks.subprocess,
            "run",
            lambda *_a, **_k: SimpleNamespace(stdout=json.dumps({"format": {"duration": "4.0"}})),
        )
        node = _node_with(ParameterVideo(name="video", pre_run_validators=[Duration(max=5)]))
        node.set_parameter_value("video", self._clip(tmp_path))
        assert self._unchecked(node) == ([], [])


class TestUncheckedLocationsByName:
    def test_names_a_file_no_declared_input_covers_by_its_file_name(self) -> None:
        assert unchecked_locations_by_name({"/clips/a.mp4", "/clips/b.mp4"}, {"/clips/a.mp4"}) == ["'b.mp4'"]

    def test_names_nothing_when_every_file_is_covered(self) -> None:
        assert unchecked_locations_by_name({"/clips/a.mp4"}, {"/clips/a.mp4"}) == []

    def test_shows_only_a_windows_paths_file_name(self) -> None:
        assert unchecked_locations_by_name({r"C:\Users\artist\Videos\clip.mp4"}, set()) == ["'clip.mp4'"]

    def test_drops_a_urls_cachebuster(self) -> None:
        location = "http://localhost:8124/workspace/staticfiles/clip.mp4?v=1730000000"
        assert unchecked_locations_by_name({location}, set()) == ["'clip.mp4'"]


class TestValuesHeldInAnotherProcess:
    """A worker-held value reaches this process only as a key; it can't be read here, so it's skipped."""

    _ELSEWHERE = make_reference(worker="some-other-worker", key="LoadVideo.video#deadbeef", source="LoadVideo@abc12345")

    def test_skips_a_single_input_held_elsewhere(self) -> None:
        node = _node_with(ParameterVideo(name="video", pre_run_validators=[Duration(max=5)]))
        node.set_parameter_value("video", self._ELSEWHERE)
        assert run_pre_run_validators(node) == []

    def test_skips_a_list_held_elsewhere(self) -> None:
        node = _node_with(
            ParameterList(
                name="clips",
                tooltip="",
                input_types=["VideoUrlArtifact"],
                type="VideoUrlArtifact",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                pre_run_validators=[MaxCount(1), Duration(max=5)],
            )
        )
        node.set_parameter_value("clips", [self._ELSEWHERE, self._ELSEWHERE])
        assert run_pre_run_validators(node) == []


class TestUncheckedInputsMessage:
    def test_keeps_a_file_names_capitals_after_the_first_input(self) -> None:
        assert unchecked_inputs_message(["The video in 'Video'", "'Own Clip.mp4'"]).startswith(
            "The video in 'Video' and 'Own Clip.mp4' weren't checked"
        )

    def test_one_input(self) -> None:
        assert unchecked_inputs_message(["The video in 'Video'"]) == (
            "The video in 'Video' wasn't checked before the run because reading it took too long. "
            "The provider may still reject it."
        )

    def test_several_inputs(self) -> None:
        assert unchecked_inputs_message(
            ["The video in 'Video'", "Audio clip 1 in 'Reference Audio'", "Audio clip 2 in 'Reference Audio'"]
        ) == (
            "The video in 'Video', audio clip 1 in 'Reference Audio' and audio clip 2 in 'Reference Audio' "
            "weren't checked before the run because reading them took too long. The provider may still reject them."
        )


@dataclass(frozen=True)
class _Records(PreRunValidator):
    seen: ClassVar[list[Any]] = []

    def check(self, value: Any, *, subject: str, media: MediaInfo, docs_url: str | None) -> NodeError | None:  # noqa: ARG002
        _Records.seen.append(value)
        return None


class _Group(SubflowNodeGroup):
    async def aprocess(self) -> None:  # pragma: no cover - execution not exercised here
        await self.execute_subflow()

    def process(self) -> Any:  # pragma: no cover - execution not exercised here
        return None


class TestNodeInAGroup:
    """A group exposes an inner parameter as a proxy; the inner node runs its own validators."""

    def test_the_proxy_carries_no_validators_and_the_inner_node_checks_its_value(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        inner = _node_with(ParameterImage(name="image", pre_run_validators=[SideRange(min=300)]))
        original = inner.get_parameter_by_name("image") or pytest.fail("no image parameter")
        group = _Group(name="group")
        engine.object_manager.add_object_by_name(group.name, group)
        try:
            # The request a group adds a proxy with for an incoming connection.
            result = engine.handle_request(
                AddParameterToNodeRequest(
                    node_name=group.name,
                    parameter_name=original.name,
                    input_types=original.input_types,
                    tooltip="",
                    mode_allowed_input=True,
                    mode_allowed_output=True,
                    serializable=original.serializable,
                )
            )
            assert isinstance(result, AddParameterToNodeResultSuccess), result
            proxy = group.get_parameter_by_name(result.parameter_name) or pytest.fail("no proxy parameter")
            image = str(image_file(tmp_path, 200, 200))
            group.set_parameter_value(proxy.name, image)
            inner.set_parameter_value("image", image)

            assert proxy.pre_run_validators == ()
            assert run_pre_run_validators(group) == []
            assert _messages(run_pre_run_validators(inner)) == [
                "The image in 'image' is 200x200 and each side must be at least 300 pixels. Use a larger one."
            ]
        finally:
            engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))


def test_media_info_reports_nothing_it_was_not_given() -> None:
    """An empty reading has no frame, format or displayed shape."""
    assert MediaInfo().frame is None
    assert MediaInfo().mime_type is None
    assert MediaInfo().display_frame is None
    assert MediaNeed("size") is MediaNeed.SIZE
