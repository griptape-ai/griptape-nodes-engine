# ruff: noqa: PLR2004

"""Tests for running a node's declared pre-run validators at its turn."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar, override

import pytest

from griptape_nodes.exe_types import input_checks
from griptape_nodes.exe_types.core_types import Parameter, ParameterList, ParameterMode
from griptape_nodes.exe_types.input_check_budget import probe_deadline
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
    ValidationResult,
    unchecked_inputs_message,
    unchecked_locations_by_name,
    validate_inputs,
    validate_node,
)
from griptape_nodes.exe_types.validators.base import EMPTY_MEDIA, MediaInfo, MediaNeed, PreRunValidator, reads
from griptape_nodes.exe_types.validators.common import IfParameter, MaxCount
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
    from collections.abc import Sequence

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
        assert validate_inputs(node) == []

    def test_names_the_input_by_its_display_name_and_kind(self, tmp_path: Path) -> None:
        node = _node_with(
            ParameterImage(name="image", display_name="Start Frame", pre_run_validators=[SideRange(min=300)])
        )
        node.set_parameter_value("image", str(image_file(tmp_path, 200, 200)))
        assert _messages(validate_inputs(node)) == [
            "The image in 'Start Frame' is 200x200 and each side must be at least 300 pixels. Use a larger one."
        ]

    def test_falls_back_to_the_parameter_name(self, tmp_path: Path) -> None:
        node = _node_with(ParameterImage(name="image_tail", pre_run_validators=[SideRange(min=300)]))
        node.set_parameter_value("image_tail", str(image_file(tmp_path, 200, 200)))
        (message,) = _messages(validate_inputs(node))
        assert message.startswith("The image in 'image_tail' is 200x200")

    def test_names_audio_as_an_audio_clip(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        clip = tmp_path / "clip.mp3"
        clip.write_bytes(b"audio")
        probe = input_checks.AudioProbe(mime_type="audio/mpeg", codec="mp3", profile=None, seconds=9.0)
        monkeypatch.setattr(input_checks, "_probe_audio", lambda _location: probe)
        node = _node_with(ParameterAudio(name="voice", pre_run_validators=[Duration(max=5)]))
        node.set_parameter_value("voice", str(clip))
        assert _messages(validate_inputs(node)) == [
            "The audio clip in 'voice' is 9.00 seconds long and the limit is 5. Trim it in an audio editor."
        ]

    def test_links_the_nodes_input_limits_page(self, tmp_path: Path) -> None:
        node = _LimitedNode(name="node")
        node.add_parameter(ParameterImage(name="image", pre_run_validators=[SideRange(min=300)]))
        node.set_parameter_value("image", str(image_file(tmp_path, 200, 200)))
        (error,) = validate_inputs(node)
        assert isinstance(error, NodeError)
        assert error.links
        assert error.links[0].url == DOCS

    def test_skips_an_empty_input(self) -> None:
        node = _node_with(ParameterImage(name="image", pre_run_validators=[SideRange(min=300)]))
        assert validate_inputs(node) == []

    def test_collects_every_error_from_every_parameter(self, tmp_path: Path) -> None:
        node = _node_with(
            ParameterImage(name="first", pre_run_validators=[SideRange(min=300), AspectRatioRange("1:1", "1:1")]),
            ParameterImage(name="second", pre_run_validators=[AllowedFormats("image/jpeg")]),
        )
        node.set_parameter_value("first", str(image_file(tmp_path, 200, 100)))
        node.set_parameter_value("second", str(image_file(tmp_path, 400, 400)))
        assert _messages(validate_inputs(node)) == [
            "The image in 'first' is 200x100 and each side must be at least 300 pixels. Use a larger one.",
            "The image in 'first' is 200x100, which is too wide. The aspect ratio must be between 1:1 and 1:1. Crop it.",
            "The image in 'second' is in PNG format. Convert it to JPEG.",
        ]

    def test_runs_a_custom_validator_on_a_string_parameter(self) -> None:
        @dataclass(frozen=True)
        class NotEmpty(PreRunValidator):
            @override
            def check(
                self,
                value: Any,
                *,
                subject: str,
                docs_url: str | None,
                media: MediaInfo = EMPTY_MEDIA,
                position: int | None = None,
            ) -> NodeError | None:
                if value.strip():
                    return None
                return NodeError(f"{subject} is blank. Write a prompt.")

        node = _node_with(ParameterString(name="prompt", pre_run_validators=[NotEmpty()]))
        node.set_parameter_value("prompt", "   ")
        assert _messages(validate_inputs(node)) == ["The input in 'prompt' is blank. Write a prompt."]


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
            messages = _messages(validate_inputs(node))
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
        assert len(validate_inputs(node)) == 1
        assert video_reads == []

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
        assert _messages(validate_inputs(node)) == [
            "Image 2 in 'reference images' is 200x1000 and each side must be at least 300 pixels. Use a larger one."
        ]

    def test_checks_items_set_row_by_row(self, tmp_path: Path) -> None:
        node = self._list_node(SideRange(min=300))
        parameter = node.get_parameter_by_name("reference_images")
        assert isinstance(parameter, ParameterList)
        for size in (100, 400, 200):
            row = parameter.add_child_parameter()
            node.set_parameter_value(row.name, str(image_file(tmp_path, size, size)))
        messages = _messages(validate_inputs(node))
        assert [message.split(" is ")[0] for message in messages] == [
            "Image 1 in 'reference images'",
            "Image 3 in 'reference images'",
        ]

    def test_counts_the_list_with_max_count(self, tmp_path: Path) -> None:
        node = self._list_node(MaxCount(2, noun="images"), SideRange(min=1))
        images = [str(image_file(tmp_path, 10 + i, 10)) for i in range(4)]
        node.set_parameter_value("reference_images", images)
        assert _messages(validate_inputs(node)) == [
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
            assert len(validate_inputs(node)) == 2
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
        assert _messages(validate_inputs(node)) == [
            "Image 2 in 'reference images' is 200x1000 and each side must be at least 300 pixels. Use a larger one."
        ]

    @pytest.mark.parametrize("value", [None, "not a list"], ids=["unset", "not-a-list"])
    def test_checks_a_value_that_is_not_a_list_as_empty(self, value: Any) -> None:
        node = self._node(SideRange(min=300), _Records())
        _Records.seen.clear()
        node.set_parameter_value("reference_images", value)
        assert validate_inputs(node) == []
        assert _Records.seen == []


class TestIfParameter:
    @staticmethod
    def _node() -> MockNode:
        return _node_with(
            ParameterString(name="model", default_value="pro"),
            ParameterImage(
                name="image",
                pre_run_validators=[
                    AllowedFormats("image/png"),
                    IfParameter(
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
        messages = _messages(validate_inputs(node))
        assert len(messages) == len(expected)
        for message, fragment in zip(messages, expected, strict=True):
            assert fragment in message

    def test_refuses_a_validator_the_parameter_cannot_hold_inside_a_nested_if_parameter(self) -> None:
        with pytest.raises(TypeError, match="checks audio, and the parameter holds image"):
            ParameterImage(
                name="image",
                pre_run_validators=[
                    IfParameter("model", {"pro": [IfParameter("size", {"large": [AudioCodecs("mp3")]})]})
                ],
            )

    def test_checks_nothing_when_the_pick_lists_no_validators(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A model that doesn't send the input lists no limits for it, and the input isn't read.
        node = _node_with(
            ParameterString(name="model", default_value="fast"),
            ParameterImage(name="last_frame", pre_run_validators=[IfParameter("model", {"pro": [SideRange(min=300)]})]),
        )
        node.set_parameter_value("last_frame", str(image_file(tmp_path, 10, 10)))
        reads: list[str] = []
        monkeypatch.setattr(input_checks, "_read_image_info", reads.append)
        with input_check_readings():
            assert validate_inputs(node) == []
        assert reads == []

    def test_refuses_a_picked_validator_the_parameter_cannot_hold(self) -> None:
        with pytest.raises(TypeError, match="checks audio, and the parameter holds image"):
            ParameterImage(name="image", pre_run_validators=[IfParameter("model", {"pro": [AudioCodecs("mp3")]})])

    def test_survives_a_parameter_copy(self) -> None:
        parameter = ParameterImage(name="image", pre_run_validators=[IfParameter("model", {"pro": [SideRange(min=1)]})])
        assert parameter.copy().pre_run_validators == parameter.pre_run_validators


@reads(MediaNeed.IMAGE)
def _no_image_read(media: MediaInfo) -> bool:
    return media.image is None


@reads(MediaNeed.IMAGE)
def _is_png(media: MediaInfo) -> bool:
    return media.mime_type == "image/png"


def _always(_media: MediaInfo) -> bool:
    return True


class TestConditions:
    """`when` and `unless` narrow a validator by the file, and `first` by a list item's position."""

    @staticmethod
    def _image_node(*validators: PreRunValidator, model: str = "pro") -> MockNode:
        node = _node_with(
            ParameterString(name="model", default_value=model),
            ParameterImage(name="image", pre_run_validators=validators),
        )
        node.set_parameter_value("model", model)
        return node

    @pytest.mark.parametrize(("image_format", "expected"), [("PNG", 1), ("JPEG", 0)])
    def test_when_applies_only_while_the_condition_holds_for_the_file(
        self, tmp_path: Path, image_format: str, expected: int
    ) -> None:
        node = self._image_node(SideRange(min=300).when(_is_png))
        node.set_parameter_value("image", str(image_file(tmp_path, 200, 200, image_format)))
        assert len(validate_inputs(node)) == expected

    @pytest.mark.parametrize(("image_format", "expected"), [("PNG", 0), ("JPEG", 1)])
    def test_unless_applies_only_while_the_condition_does_not_hold(
        self, tmp_path: Path, image_format: str, expected: int
    ) -> None:
        node = self._image_node(SideRange(min=300).unless(_is_png))
        node.set_parameter_value("image", str(image_file(tmp_path, 200, 200, image_format)))
        assert len(validate_inputs(node)) == expected

    def test_a_condition_sees_what_was_read(self, tmp_path: Path) -> None:
        node = self._image_node(MaxFileSize(1).unless(_no_image_read))
        node.set_parameter_value("image", str(image_file(tmp_path, 20, 20)))
        assert len(validate_inputs(node)) == 1
        unreadable = tmp_path / "not-an-image.png"
        unreadable.write_bytes(b"not an image")
        node.set_parameter_value("image", str(unreadable))
        assert validate_inputs(node) == []

    def test_reads_what_a_condition_declares_along_with_the_validators_needs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reads_seen: list[str] = []

        def counting(location: str) -> ImageInfo:
            reads_seen.append(location)
            return ImageInfo(10, 10, "image/png")

        monkeypatch.setattr(input_checks, "_read_image_info", counting)
        wrapped = MaxFileSize(1).when(_no_image_read)
        assert wrapped.needs == {MediaNeed.SIZE, MediaNeed.IMAGE}
        node = self._image_node(wrapped)
        image = str(image_file(tmp_path, 10, 10))
        node.set_parameter_value("image", image)
        with input_check_readings():
            # The image was read for the condition, which then held false, so the size limit was skipped.
            assert validate_inputs(node) == []
        assert reads_seen == [image]

    def test_a_condition_without_declared_needs_reads_only_the_validators(self) -> None:
        assert SideRange(min=1).when(_always).needs == SideRange.needs

    def test_wraps_each_pick_of_a_if_parameter(self, tmp_path: Path) -> None:
        by_model = IfParameter("model", {"pro": [SideRange(min=300)]}, default=[SideRange(min=1000)])
        node = self._image_node(by_model.unless(_no_image_read), model="pro")
        node.set_parameter_value("image", str(image_file(tmp_path, 200, 200)))
        assert _messages(validate_inputs(node)) == [
            "The image in 'image' is 200x200 and each side must be at least 300 pixels. Use a larger one."
        ]

    def test_an_if_parameter_can_hold_a_conditional_validator(self, tmp_path: Path) -> None:
        node = self._image_node(IfParameter("model", {"pro": [SideRange(min=300).when(_is_png)]}), model="pro")
        node.set_parameter_value("image", str(image_file(tmp_path, 200, 200, "JPEG")))
        assert validate_inputs(node) == []
        node.set_parameter_value("image", str(image_file(tmp_path, 200, 200)))
        assert len(validate_inputs(node)) == 1
        node.set_parameter_value("model", "lite")
        assert validate_inputs(node) == []

    def test_first_checks_only_the_first_items_of_a_list(self, tmp_path: Path) -> None:
        node = TestLists._list_node(SideRange(min=300).first(2), MaxCount(3, noun="images").first(1))
        images = [str(image_file(tmp_path, 200 + i, 200)) for i in range(4)]
        node.set_parameter_value("reference_images", images)
        # A whole-list validator has no position, so `first` doesn't narrow it.
        assert [message.split(" is ")[0].split(" are ")[0] for message in _messages(validate_inputs(node))] == [
            "4 images",
            "Image 1 in 'reference images'",
            "Image 2 in 'reference images'",
        ]

    def test_first_applies_to_a_single_input(self, tmp_path: Path) -> None:
        node = self._image_node(SideRange(min=300).first(1))
        node.set_parameter_value("image", str(image_file(tmp_path, 200, 200)))
        assert len(validate_inputs(node)) == 1

    def test_first_refuses_a_count_below_one(self) -> None:
        with pytest.raises(ValueError, match="first 0 items"):
            SideRange(min=1).first(0)

    @pytest.mark.parametrize(
        ("build", "match"),
        [
            (lambda: ParameterImage(name="p", pre_run_validators=[Duration(max=1).when(_always)]), "Duration"),
            (
                lambda: ParameterImage(
                    name="p", pre_run_validators=[IfParameter("m", {"x": [AudioCodecs("mp3")]}).first(1)]
                ),
                "AudioCodecs",
            ),
            (lambda: ParameterImage(name="p", pre_run_validators=[MaxCount(2).unless(_always)]), "a list"),
            (
                lambda: ParameterVideo(name="p", pre_run_validators=[MaxFileSize(1).when(_no_image_read)]),
                "checks image, and the parameter holds video",
            ),
        ],
        ids=["conditional", "first-of-if-parameter", "whole-list-on-single", "condition-reads"],
    )
    def test_the_build_time_fit_check_sees_through_wrappers(self, build: Any, match: str) -> None:
        with pytest.raises(TypeError, match=match):
            build()

    def test_survives_a_parameter_copy(self) -> None:
        parameter = ParameterImage(name="image", pre_run_validators=[SideRange(min=1).when(_no_image_read).first(2)])
        assert parameter.copy().pre_run_validators == parameter.pre_run_validators


class TestRulesAcrossInputs:
    """A rule across several inputs goes in `validate_before_node_run`, which reuses what the validators read."""

    class _PairNode(_LimitedNode):
        def __init__(self) -> None:
            super().__init__(name="node")
            self.add_parameter(ParameterImage(name="first", pre_run_validators=[SideRange(min=300)]))
            self.add_parameter(ParameterImage(name="second", pre_run_validators=[SideRange(min=1)]))

        def validate_before_node_run(self) -> list[Exception] | None:
            first = local_image_info(self.get_parameter_value("first"))
            second = local_image_info(self.get_parameter_value("second"))
            if first is None or second is None or first[:2] == second[:2]:
                return None
            return [NodeError("'first' and 'second' must be the same size.")]

    def test_runs_after_the_validators_and_reuses_their_readings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reads: list[str] = []
        sizes = {"a.png": 200, "b.png": 400}

        def counting(location: str) -> ImageInfo:
            reads.append(location)
            side = sizes[Path(location).name]
            return ImageInfo(side, side, "image/png")

        monkeypatch.setattr(input_checks, "_read_image_info", counting)
        node = self._PairNode()
        first, second = str(tmp_path / "a.png"), str(tmp_path / "b.png")
        node.set_parameter_value("first", first)
        node.set_parameter_value("second", second)
        assert _messages(validate_node(node).errors) == [
            "The image in 'first' is 200x200 and each side must be at least 300 pixels. Use a larger one.",
            "'first' and 'second' must be the same size.",
        ]
        assert reads == [first, second]


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
        validate_inputs(node)
        assert _Records.seen == ["a cat"]


@pytest.fixture
def slow_ffprobe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every ffprobe run time out."""

    def run(cmd: list[str], **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(input_checks, "_installed_ffprobe", lambda: "/usr/bin/ffprobe")
    monkeypatch.setattr(input_checks.subprocess, "run", run)


def _clip(tmp_path: Path, name: str = "clip.mp4") -> str:
    clip = tmp_path / name
    clip.write_bytes(b"video")
    return str(clip)


@pytest.mark.usefixtures("slow_ffprobe")
class TestUncheckedInputs:
    """An input that took too long to read is named, so the user can be told; the node still runs."""

    @staticmethod
    def _unchecked(node: BaseNode) -> tuple[list[NodeError], list[UncheckedInput]]:
        unchecked: list[UncheckedInput] = []
        with input_check_readings():
            errors = validate_inputs(node, unchecked=unchecked)
        return errors, unchecked

    def test_names_a_video_that_took_too_long(self, tmp_path: Path) -> None:
        clip = _clip(tmp_path)
        node = _node_with(ParameterVideo(name="video", display_name="Video", pre_run_validators=[Duration(max=5)]))
        node.set_parameter_value("video", clip)
        assert self._unchecked(node) == ([], [(clip, "The video in 'Video'")])

    def test_still_reports_what_it_could_read(self, tmp_path: Path) -> None:
        clip = _clip(tmp_path)
        node = _node_with(ParameterVideo(name="video", pre_run_validators=[MaxFileSize(1), Duration(max=5)]))
        node.set_parameter_value("video", clip)
        errors, unchecked = self._unchecked(node)
        (message,) = _messages(errors)
        assert message.startswith("The video in 'video' is")
        assert unchecked == [(clip, "The video in 'video'")]

    def test_names_each_list_item_by_its_position(self, tmp_path: Path) -> None:
        first, second = _clip(tmp_path, "a.mp4"), _clip(tmp_path, "b.mp4")
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
        clip = _clip(tmp_path)
        node = _node_with(
            ParameterVideo(name="start", display_name="Start", pre_run_validators=[Duration(max=5)]),
            ParameterVideo(name="end", display_name="End", pre_run_validators=[Duration(max=5)]),
        )
        node.set_parameter_value("start", clip)
        node.set_parameter_value("end", clip)
        assert self._unchecked(node)[1] == [(clip, "The video in 'Start'"), (clip, "The video in 'End'")]

    def test_does_not_name_an_input_sharing_the_file_whose_checks_all_ran(self, tmp_path: Path) -> None:
        clip = _clip(tmp_path)
        node = _node_with(
            ParameterVideo(name="start", display_name="Start", pre_run_validators=[Duration(max=5)]),
            ParameterVideo(name="end", display_name="End", pre_run_validators=[MaxFileSize(10_000)]),
        )
        node.set_parameter_value("start", clip)
        node.set_parameter_value("end", clip)
        assert self._unchecked(node)[1] == [(clip, "The video in 'Start'")]

    def test_names_nothing_when_only_the_size_is_read(self, tmp_path: Path) -> None:
        node = _node_with(ParameterVideo(name="video", pre_run_validators=[MaxFileSize(10_000)]))
        node.set_parameter_value("video", _clip(tmp_path))
        assert self._unchecked(node) == ([], [])

    def test_names_nothing_for_an_input_read_in_time(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            input_checks.subprocess,
            "run",
            lambda *_a, **_k: SimpleNamespace(stdout=json.dumps({"format": {"duration": "4.0"}})),
        )
        node = _node_with(ParameterVideo(name="video", pre_run_validators=[Duration(max=5)]))
        node.set_parameter_value("video", _clip(tmp_path))
        assert self._unchecked(node) == ([], [])


class TestValidateNode:
    """One call runs the declared validators and the node's own checks, as the engine does at its turn."""

    class _OwnChecks(MockNode):
        """A node with a declared limit on its video, and its own check that may read a file and fail."""

        def __init__(self, *, own_errors: list[Exception] | None = None, reads: str | None = None) -> None:
            super().__init__(name="node")
            self._own_errors = own_errors
            self._reads = reads
            self.add_parameter(
                ParameterVideo(name="video", display_name="Video", pre_run_validators=[MaxFileSize(1), Duration(max=5)])
            )

        def validate_before_node_run(self) -> list[Exception] | None:
            if self._reads is not None:
                local_video_probe(self._reads)
            return self._own_errors

    def test_reports_validator_errors_then_the_nodes_own(self, tmp_path: Path) -> None:
        own = ValueError("A prompt is required.")
        node = self._OwnChecks(own_errors=[own])
        node.set_parameter_value("video", _clip(tmp_path))
        result = validate_node(node)
        assert len(result.errors) == 2
        assert str(result.errors[0]).startswith("The video in 'Video' is")
        assert result.errors[1] is own

    def test_reports_nothing_for_a_node_that_passes(self) -> None:
        assert validate_node(self._OwnChecks()) == ValidationResult(errors=[], unchecked_inputs=[])

    @pytest.mark.usefixtures("slow_ffprobe")
    def test_names_an_input_whose_probe_timed_out(self, tmp_path: Path) -> None:
        node = self._OwnChecks()
        node.set_parameter_value("video", _clip(tmp_path))
        assert validate_node(node).unchecked_inputs == ["The video in 'Video'"]

    def test_names_nothing_when_every_input_was_read_in_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(input_checks, "_installed_ffprobe", lambda: "/usr/bin/ffprobe")
        monkeypatch.setattr(
            input_checks.subprocess,
            "run",
            lambda *_a, **_k: SimpleNamespace(stdout=json.dumps({"format": {"duration": "4.0"}})),
        )
        node = self._OwnChecks()
        node.set_parameter_value("video", _clip(tmp_path))
        assert validate_node(node).unchecked_inputs == []

    @pytest.mark.usefixtures("slow_ffprobe")
    def test_names_a_file_the_nodes_own_check_could_not_read(self, tmp_path: Path) -> None:
        node = self._OwnChecks(reads=_clip(tmp_path, "own.mp4"))
        assert validate_node(node).unchecked_inputs == ["'own.mp4'"]

    @pytest.mark.usefixtures("slow_ffprobe")
    def test_names_a_declared_input_once_when_the_node_also_reads_it(self, tmp_path: Path) -> None:
        clip = _clip(tmp_path)
        node = self._OwnChecks(reads=clip)
        node.set_parameter_value("video", clip)
        assert validate_node(node).unchecked_inputs == ["The video in 'Video'"]

    @pytest.mark.usefixtures("slow_ffprobe")
    def test_reports_unchecked_inputs_alongside_errors(self, tmp_path: Path) -> None:
        # The machine decides not to warn a node that failed; the result still carries both.
        node = self._OwnChecks(own_errors=[ValueError("A prompt is required.")])
        node.set_parameter_value("video", _clip(tmp_path))
        result = validate_node(node)
        assert _messages(result.errors)[-1] == "A prompt is required."
        assert result.unchecked_inputs == ["The video in 'Video'"]

    def test_runs_the_nodes_own_checks_inside_the_budget_and_the_shared_readings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reads: list[str] = []

        def counting_probe(location: str) -> VideoProbe:
            reads.append(location)
            return VideoProbe(info=ImageInfo(100, 100, None), seconds=4.0, fps=24.0, mime_type="video/mp4")

        monkeypatch.setattr(input_checks, "_probe_video", counting_probe)
        deadlines: list[float | None] = []
        clip = _clip(tmp_path)

        class Records(self._OwnChecks):
            def validate_before_node_run(self) -> list[Exception] | None:
                deadlines.append(probe_deadline())
                return super().validate_before_node_run()

        node = Records(reads=clip)
        node.set_parameter_value("video", clip)
        validate_node(node)
        assert deadlines[0] is not None
        # The validators' probe and the node's own share one reading.
        assert reads == [clip]
        # Both are closed once the call returns.
        assert probe_deadline() is None
        local_video_probe(clip)
        assert reads == [clip, clip]


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
        assert validate_inputs(node) == []

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
        assert validate_inputs(node) == []


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

    @override
    def check(
        self,
        value: Any,
        *,
        subject: str,
        docs_url: str | None,
        media: MediaInfo = EMPTY_MEDIA,
        position: int | None = None,
    ) -> NodeError | None:
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
            assert validate_inputs(group) == []
            assert _messages(validate_inputs(inner)) == [
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
