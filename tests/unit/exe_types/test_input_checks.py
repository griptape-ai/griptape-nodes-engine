# ruff: noqa: PLR2004

"""Tests for the checks nodes run on their media inputs before a run."""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import sys
from io import BytesIO
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from griptape.artifacts import ImageArtifact, ImageUrlArtifact
from griptape.artifacts.video_url_artifact import VideoUrlArtifact
from PIL import Image, PngImagePlugin, features

from griptape_nodes.exe_types import input_checks
from griptape_nodes.exe_types.input_checks import (
    AudioProbe,
    ImageInfo,
    VideoProbe,
    check_aspect_ratio_exact,
    check_aspect_ratio_range,
    check_audio_codec,
    check_count,
    check_dimensions,
    check_duration,
    check_frame_count,
    check_frame_rate,
    check_mime_type,
    check_size,
    check_video_codec,
    input_check_budget,
    input_check_readings,
    input_subject,
    local_audio_probe,
    local_image_format,
    local_image_info,
    local_size_bytes,
    local_video_probe,
    read_media,
)
from griptape_nodes.exe_types.node_error import NodeError
from griptape_nodes.exe_types.validators.base import MediaInfo, MediaNeed
from tests.unit.exe_types.validators.media_files import HEIC_PHOTO

if TYPE_CHECKING:
    from pathlib import Path


def _probed_seconds(value: Any) -> float | None:
    """The stream duration `local_video_probe` reads, or None when it reads nothing."""
    probe = local_video_probe(value)
    if probe is None:
        return None
    return probe.seconds


def _probed_frame(value: Any) -> ImageInfo | None:
    """The frame size `local_video_probe` reads, or None when it reads nothing."""
    probe = local_video_probe(value)
    if probe is None:
        return None
    return probe.info


SUBJECT = "Image 2 in 'reference images'"
DOCS = "https://example.com/limits"


def _png_bytes(width: int, height: int) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height)).save(buffer, format="PNG")
    return buffer.getvalue()


def _rotated_jpeg_bytes(width: int, height: int, orientation: int, image_format: str = "JPEG") -> bytes:
    """An image stored `width`x`height` with an EXIF Orientation tag, as phones save a rotated shot."""
    exif = Image.Exif()
    exif[0x0112] = orientation
    buffer = BytesIO()
    Image.new("RGB", (width, height)).save(buffer, format=image_format, exif=exif.tobytes())
    return buffer.getvalue()


def _data_uri(data: bytes, mime_type: str = "image/png") -> str:
    return f"data:{mime_type};base64,{base64.b64encode(data).decode()}"


class TestInputSubject:
    def test_names_a_position_in_a_list(self) -> None:
        assert input_subject("reference images", noun="image", position=2) == SUBJECT

    def test_names_a_single_input(self) -> None:
        assert input_subject("start frame", noun="image") == "The image in 'start frame'"

    def test_keeps_a_nouns_own_capitals_at_a_position(self) -> None:
        assert input_subject("icons", noun="SVG icon", position=2) == "SVG icon 2 in 'icons'"
        assert input_subject("loras", noun="LoRA file", position=1) == "LoRA file 1 in 'loras'"


class TestLocalSizeBytes:
    @pytest.mark.parametrize("length", [1, 2, 3, 100, 1001])
    def test_measures_a_data_uri_exactly(self, length: int) -> None:
        assert local_size_bytes(_data_uri(b"x" * length), kind="image") == length

    @pytest.mark.parametrize("length", [1, 2, 3, 100, 1001])
    def test_leaves_line_breaks_out_of_a_data_uri_size(self, length: int) -> None:
        payload = base64.b64encode(b"x" * length).decode()
        wrapped = "\n".join(payload[i : i + 76] for i in range(0, len(payload), 76)) + "\n"
        assert local_size_bytes(f"data:image/png;base64,{wrapped}", kind="image") == length

    def test_ignores_line_breaks_in_a_data_uri(self) -> None:
        wrapped = f"data:image/png;base64,{base64.encodebytes(b'x' * 1000).decode()}"
        assert local_size_bytes(wrapped, kind="image") == 1000

    def test_measures_a_local_file(self, tmp_path: Path) -> None:
        path = tmp_path / "image.png"
        path.write_bytes(b"x" * 1234)
        assert local_size_bytes(ImageUrlArtifact(value=str(path)), kind="image") == 1234

    def test_skips_a_remote_url(self) -> None:
        assert local_size_bytes("https://example.com/image.png", kind="image") is None

    def test_skips_a_path_the_os_refuses(self) -> None:
        assert local_size_bytes("A" * 3000, kind="image") is None

    def test_skips_a_missing_file(self, tmp_path: Path) -> None:
        assert local_size_bytes(str(tmp_path / "missing.png"), kind="image") is None

    def test_skips_a_file_it_cannot_stat(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = tmp_path / "image.png"
        path.write_bytes(b"x" * 10)
        monkeypatch.setattr(input_checks, "_local_path", lambda _location: tmp_path / "deleted-since.png")
        assert local_size_bytes(str(path), kind="image") is None

    def test_skips_an_empty_value(self) -> None:
        assert local_size_bytes(None, kind="image") is None


class TestLocalImageInfo:
    def test_reads_a_data_uri(self) -> None:
        assert local_image_info(_data_uri(_png_bytes(40, 30))) == ImageInfo(40, 30, "image/png")

    @pytest.mark.parametrize("orientation", [5, 6, 7, 8])
    def test_reports_an_exif_rotated_photo_as_displayed(self, orientation: int) -> None:
        image = _data_uri(_rotated_jpeg_bytes(400, 300, orientation), "image/jpeg")
        assert local_image_info(image) == ImageInfo(300, 400, "image/jpeg")

    @pytest.mark.parametrize("orientation", [1, 2, 3, 4])
    def test_keeps_the_stored_size_when_exif_does_not_rotate_a_quarter_turn(self, orientation: int) -> None:
        image = _data_uri(_rotated_jpeg_bytes(400, 300, orientation), "image/jpeg")
        assert local_image_info(image) == ImageInfo(400, 300, "image/jpeg")

    @pytest.mark.parametrize(("image_format", "mime_type"), [("PNG", "image/png"), ("TIFF", "image/tiff")])
    def test_reads_exif_rotation_from_other_formats(self, image_format: str, mime_type: str) -> None:
        image = _data_uri(_rotated_jpeg_bytes(400, 300, 6, image_format), mime_type)
        assert local_image_info(image) == ImageInfo(300, 400, mime_type)

    @pytest.mark.skipif(not features.check("avif"), reason="this Pillow build has no AVIF support")
    def test_skips_a_damaged_avif(self) -> None:
        # Pillow's AVIF plugin passes on libavif's RuntimeError for a damaged file.
        buffer = BytesIO()
        Image.new("RGB", (64, 64)).save(buffer, format="AVIF")
        data = buffer.getvalue()
        item_locations = data.index(b"iloc") + 4
        damaged = data[:item_locations] + bytes(16) + data[item_locations + 16 :]
        assert local_image_info(_data_uri(damaged, "image/avif")) is None

    def test_does_not_decode_a_png_without_exif(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Pillow's PNG plugin decodes the whole image to look for EXIF after the pixel data.
        def no_decode(*_args: Any, **_kwargs: Any) -> None:
            pytest.fail("a pre-run input check must read only the image header")

        monkeypatch.setattr(PngImagePlugin.PngImageFile, "load", no_decode)
        assert local_image_info(_data_uri(_png_bytes(400, 300))) == ImageInfo(400, 300, "image/png")

    def test_reads_a_local_file(self, tmp_path: Path) -> None:
        path = tmp_path / "image.jpg"
        Image.new("RGB", (64, 48)).save(path, format="JPEG")
        assert local_image_info(str(path)) == ImageInfo(64, 48, "image/jpeg")

    def test_reads_an_image_artifact(self) -> None:
        artifact = ImageArtifact(value=_png_bytes(8, 4), format="png", width=8, height=4)
        assert local_image_info(artifact) == ImageInfo(8, 4, "image/png")

    def test_reports_a_multi_picture_jpeg_as_jpeg(self) -> None:
        buffer = BytesIO()
        Image.new("RGB", (8, 8)).save(buffer, format="MPO", save_all=True, append_images=[Image.new("RGB", (8, 8))])
        assert local_image_info(_data_uri(buffer.getvalue(), "image/jpeg")) == ImageInfo(8, 8, "image/jpeg")

    def test_skips_an_image_too_large_to_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10)
        assert local_image_info(_data_uri(_png_bytes(40, 30))) is None

    def test_skips_a_remote_url(self) -> None:
        assert local_image_info("https://example.com/image.png") is None

    def test_skips_an_image_pillow_recognises_but_cannot_decode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def unimplemented(*_args: Any, **_kwargs: Any) -> None:
            msg = "Unimplemented DXGI format 10"
            raise NotImplementedError(msg)

        monkeypatch.setattr(input_checks.Image, "open", unimplemented)
        assert local_image_info(_data_uri(_png_bytes(8, 8))) is None

    def test_skips_bytes_that_are_not_an_image(self) -> None:
        assert local_image_info(_data_uri(b"not an image")) is None

    def test_keeps_the_stored_size_when_exif_cannot_be_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        image = _data_uri(_rotated_jpeg_bytes(400, 300, 6), "image/jpeg")

        def broken_exif(_image: Any) -> None:
            msg = "EXIF IFD runs past the end of the file"
            raise SyntaxError(msg)

        monkeypatch.setattr(input_checks.Image.Image, "getexif", broken_exif)
        assert local_image_info(image) == ImageInfo(400, 300, "image/jpeg")

    def test_skips_a_data_uri_whose_payload_is_not_base64(self) -> None:
        # A single base64 character is a truncated quantum, which can't be decoded.
        assert local_image_info("data:image/png;base64,A") is None


class TestReadersWithoutALocation:
    """A value that names no location, such as an empty artifact, reads as nothing."""

    def test_read_media_reads_nothing(self) -> None:
        assert read_media("   ", {MediaNeed.SIZE, MediaNeed.IMAGE}, kind="image") == MediaInfo()

    @pytest.mark.parametrize(
        "reader",
        [local_image_info, local_video_probe, local_audio_probe, lambda value: local_size_bytes(value, kind="image")],
        ids=["image", "video", "audio", "size"],
    )
    def test_each_reader_reads_nothing(self, reader: Any) -> None:
        assert reader(None) is None
        assert reader("") is None


class TestLocalImageFormat:
    def test_reads_a_format_pillow_opens(self) -> None:
        assert local_image_format(_data_uri(_png_bytes(8, 8))) == "image/png"

    def test_reads_a_heic_photo_from_its_signature(self) -> None:
        assert local_image_info(str(HEIC_PHOTO)) is None
        assert local_image_format(str(HEIC_PHOTO)) == "image/heif"
        assert local_image_format(_data_uri(HEIC_PHOTO.read_bytes(), "image/heic")) == "image/heif"

    @pytest.mark.parametrize(
        ("brand", "expected"),
        [
            (b"heic", "image/heif"),
            (b"heix", "image/heif"),
            (b"hevc", "image/heif"),
            (b"heim", "image/heif"),
            (b"heis", "image/heif"),
            (b"hevm", "image/heif"),
            (b"hevs", "image/heif"),
            (b"mif1", "image/heif"),
            (b"msf1", "image/heif"),
            (b"avif", "image/avif"),
            # A video's brand names no image format; the video reader judges it.
            (b"isom", None),
            (b"qt  ", None),
        ],
    )
    def test_reads_an_iso_media_brand_pillow_cannot_open(self, brand: bytes, expected: str | None) -> None:
        header = b"\x00\x00\x00\x18ftyp" + brand + b"\x00" * 12
        assert local_image_format(_data_uri(header, "image/heic")) == expected

    @pytest.mark.parametrize(
        "data",
        [b"not an image at all", b"\x00\x00\x00\x18ftyp", b"\x00\x00\x00\x18moovheic" + b"\x00" * 12],
        ids=["no-signature", "too-short", "not-ftyp"],
    )
    def test_skips_bytes_whose_signature_names_no_image(self, data: bytes) -> None:
        assert local_image_format(_data_uri(data)) is None

    def test_skips_a_data_uri_that_is_not_base64(self) -> None:
        assert local_image_format("data:image/heic;base64,@@@@not-base64@@@@") is None

    def test_skips_a_remote_url_without_fetching(self) -> None:
        assert local_image_format("https://example.com/photo.heic") is None

    def test_skips_a_value_that_names_no_location(self) -> None:
        assert local_image_format(None) is None

    @pytest.mark.skipif(sys.platform == "win32", reason="Windows has no unreadable-file permission bits")
    def test_skips_a_file_it_cannot_open(self, tmp_path: Path) -> None:
        path = tmp_path / "photo.heic"
        path.write_bytes(HEIC_PHOTO.read_bytes())
        path.chmod(0)
        try:
            if os.access(path, os.R_OK):
                pytest.skip("running as a user who can read any file")
            assert local_image_format(str(path)) is None
        finally:
            path.chmod(0o600)


class TestLocalVideoDuration:
    @pytest.fixture(autouse=True)
    def no_ffmpeg_download(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fetch(*_args: Any, **_kwargs: Any) -> None:
            pytest.fail("a pre-run input check must never fetch ffmpeg")

        monkeypatch.setattr(input_checks.static_ffmpeg_run, "get_or_fetch_platform_executables_else_raise", fetch)

    @pytest.fixture
    def clip(self, tmp_path: Path) -> Path:
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"video")
        return path

    @pytest.fixture
    def ffprobe_runs(self, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
        """Pretend ffprobe is installed and answer every run with a 9.5 second duration."""
        runs: list[dict[str, Any]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
            runs.append({"cmd": cmd, **kwargs})
            return SimpleNamespace(stdout=json.dumps({"format": {"duration": "9.5"}}))

        monkeypatch.setattr(input_checks, "_installed_ffprobe", lambda: "/usr/bin/ffprobe")
        monkeypatch.setattr(input_checks.subprocess, "run", fake_run)
        return runs

    def test_probes_a_local_file_with_a_short_timeout(self, clip: Path, ffprobe_runs: list[dict[str, Any]]) -> None:
        assert _probed_seconds(VideoUrlArtifact(value=str(clip))) == 9.5
        assert ffprobe_runs[0]["cmd"][0] == "/usr/bin/ffprobe"
        assert ffprobe_runs[0]["cmd"][-1] == str(clip)
        assert ffprobe_runs[0]["timeout"] == input_checks.FFPROBE_TIMEOUT_SECONDS

    def test_decodes_ffprobe_output_as_utf8(self, clip: Path, ffprobe_runs: list[dict[str, Any]]) -> None:
        # ffprobe echoes the file name in UTF-8; decoding with Windows' code page fails on a non-Latin one.
        _probed_seconds(str(clip))
        assert ffprobe_runs[0]["encoding"] == "utf-8"

    def test_gives_each_probe_the_probe_timeout_outside_a_budget(
        self, clip: Path, ffprobe_runs: list[dict[str, Any]]
    ) -> None:
        _probed_seconds(str(clip))
        assert ffprobe_runs[0]["timeout"] == input_checks.FFPROBE_TIMEOUT_SECONDS

    def test_gives_a_probe_only_what_is_left_of_the_budget(
        self, clip: Path, ffprobe_runs: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = [100.0]
        monkeypatch.setattr(input_checks.time, "monotonic", lambda: now[0])
        with input_check_budget(seconds=3):
            _probed_seconds(str(clip))
            now[0] += 2.5
            _probed_seconds(str(clip))
        assert [run["timeout"] for run in ffprobe_runs] == [input_checks.FFPROBE_TIMEOUT_SECONDS, 0.5]

    def test_skips_a_probe_once_the_budget_is_spent(
        self, clip: Path, ffprobe_runs: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = [100.0]
        monkeypatch.setattr(input_checks.time, "monotonic", lambda: now[0])
        with input_check_budget(seconds=3):
            now[0] += 3
            assert _probed_seconds(str(clip)) is None
        assert ffprobe_runs == []

    def test_lets_a_nested_budget_shorten_but_not_extend_the_outer_one(
        self, clip: Path, ffprobe_runs: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = [100.0]
        monkeypatch.setattr(input_checks.time, "monotonic", lambda: now[0])
        with input_check_budget(seconds=3):
            with input_check_budget(seconds=60):
                now[0] += 2.5
                _probed_seconds(str(clip))
            with input_check_budget(seconds=0.2):
                _probed_seconds(str(clip))
        assert [run["timeout"] for run in ffprobe_runs] == [0.5, pytest.approx(0.2)]

    def test_ends_the_budget_with_its_block(
        self, clip: Path, ffprobe_runs: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = [100.0]
        monkeypatch.setattr(input_checks.time, "monotonic", lambda: now[0])
        with input_check_budget(seconds=1):
            now[0] += 5
        assert _probed_seconds(str(clip)) == 9.5
        assert ffprobe_runs[0]["timeout"] == input_checks.FFPROBE_TIMEOUT_SECONDS

    def test_skips_when_ffprobe_times_out(self, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def slow_run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

        monkeypatch.setattr(input_checks, "_installed_ffprobe", lambda: "/usr/bin/ffprobe")
        monkeypatch.setattr(input_checks.subprocess, "run", slow_run)

        assert _probed_seconds(str(clip)) is None

    def test_skips_when_the_probe_fails(self, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def failing_run(cmd: list[str], **_kwargs: Any) -> SimpleNamespace:
            raise subprocess.CalledProcessError(1, cmd, stderr="Invalid data found")

        monkeypatch.setattr(input_checks, "_installed_ffprobe", lambda: "/usr/bin/ffprobe")
        monkeypatch.setattr(input_checks.subprocess, "run", failing_run)

        assert _probed_seconds(str(clip)) is None

    def test_skips_when_ffprobe_is_not_installed(self, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(input_checks, "_installed_ffprobe", lambda: None)
        monkeypatch.setattr(input_checks.subprocess, "run", lambda *_a, **_k: pytest.fail("ffprobe must not run"))

        assert _probed_seconds(str(clip)) is None

    @pytest.mark.parametrize("value", ["https://example.com/clip.mp4", "data:video/mp4;base64,AAAA"])
    def test_does_not_probe_a_remote_or_inline_video(self, value: str, ffprobe_runs: list[dict[str, Any]]) -> None:
        assert _probed_seconds(value) is None
        assert ffprobe_runs == []

    @pytest.mark.parametrize("stdout", ["not json", "[1, 2]", ""], ids=["not-json", "not-an-object", "empty"])
    def test_skips_when_ffprobe_prints_no_json_object(
        self, clip: Path, monkeypatch: pytest.MonkeyPatch, stdout: str
    ) -> None:
        monkeypatch.setattr(input_checks, "_installed_ffprobe", lambda: "/usr/bin/ffprobe")
        monkeypatch.setattr(input_checks.subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout=stdout))
        assert _probed_seconds(str(clip)) is None

    def test_skips_a_probe_that_times_out(self, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def slow_run(cmd: list[str], **kwargs: Any) -> None:
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

        monkeypatch.setattr(input_checks, "_installed_ffprobe", lambda: "/usr/bin/ffprobe")
        monkeypatch.setattr(input_checks.subprocess, "run", slow_run)
        assert _probed_seconds(str(clip)) is None

    def test_skips_a_path_it_cannot_resolve(self, ffprobe_runs: list[dict[str, Any]]) -> None:
        # A macro path naming a variable the project doesn't define resolves to nothing.
        assert _probed_seconds("{no_such_variable}/clip.mp4") is None
        assert ffprobe_runs == []


class TestProbeTimeouts:
    """A probe that runs out of time is noted, so the engine can say the input wasn't checked."""

    @pytest.fixture
    def clip(self, tmp_path: Path) -> Path:
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"video")
        return path

    @pytest.fixture
    def slow_ffprobe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def slow_run(cmd: list[str], **kwargs: Any) -> None:
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

        monkeypatch.setattr(input_checks, "_installed_ffprobe", lambda: "/usr/bin/ffprobe")
        monkeypatch.setattr(input_checks.subprocess, "run", slow_run)

    @pytest.mark.usefixtures("slow_ffprobe")
    def test_notes_a_probe_that_times_out(self, clip: Path, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="griptape_nodes"), input_check_readings():
            info = read_media(str(clip), {MediaNeed.VIDEO}, kind="video")
        assert info.video is None
        assert info.timed_out
        assert any(
            "ffprobe took longer than 2 seconds" in record.getMessage() and str(clip) in record.getMessage()
            for record in caplog.records
        )

    def test_notes_a_probe_the_budget_leaves_no_time_for(
        self, clip: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        now = [100.0]
        monkeypatch.setattr(input_checks.time, "monotonic", lambda: now[0])
        monkeypatch.setattr(input_checks, "_installed_ffprobe", lambda: "/usr/bin/ffprobe")
        monkeypatch.setattr(input_checks.subprocess, "run", lambda *_a, **_k: pytest.fail("ffprobe must not run"))
        with (
            caplog.at_level(logging.WARNING, logger="griptape_nodes"),
            input_check_budget(seconds=3),
            input_check_readings(),
        ):
            now[0] += 3
            info = read_media(str(clip), {MediaNeed.AUDIO}, kind="audio")
        assert info.timed_out
        assert any("had already used their time" in record.getMessage() for record in caplog.records)

    @pytest.mark.usefixtures("slow_ffprobe")
    def test_notes_only_the_input_that_timed_out(self, clip: Path, tmp_path: Path) -> None:
        other = tmp_path / "other.mp4"
        other.write_bytes(b"video")
        with input_check_readings():
            read_media(str(clip), {MediaNeed.VIDEO}, kind="video")
            assert not read_media(str(other), {MediaNeed.SIZE}, kind="video").timed_out
            # Read again from the block's readings, it still says the probe timed out.
            assert read_media(str(clip), {MediaNeed.VIDEO}, kind="video").timed_out

    @pytest.mark.usefixtures("slow_ffprobe")
    def test_forgets_timeouts_when_the_block_ends(self, clip: Path) -> None:
        with input_check_readings():
            read_media(str(clip), {MediaNeed.VIDEO}, kind="video")
        with input_check_readings():
            assert read_media(str(clip), {MediaNeed.SIZE}, kind="video").timed_out is False
        assert read_media(str(clip), {MediaNeed.VIDEO}, kind="video").timed_out is False

    def test_does_not_note_a_probe_that_fails(self, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def failing_run(cmd: list[str], **_kwargs: Any) -> None:
            raise subprocess.CalledProcessError(1, cmd, stderr="Invalid data found")

        monkeypatch.setattr(input_checks, "_installed_ffprobe", lambda: "/usr/bin/ffprobe")
        monkeypatch.setattr(input_checks.subprocess, "run", failing_run)
        with input_check_readings():
            assert not read_media(str(clip), {MediaNeed.VIDEO}, kind="video").timed_out

    def test_does_not_note_a_missing_ffprobe(self, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(input_checks, "_installed_ffprobe", lambda: None)
        with input_check_readings():
            assert not read_media(str(clip), {MediaNeed.VIDEO}, kind="video").timed_out


class TestLocalVideoProbe:
    @pytest.fixture
    def clip(self, tmp_path: Path) -> Path:
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"video")
        return path

    @pytest.mark.parametrize(
        ("stream", "expected_fps"),
        [
            ({"avg_frame_rate": "0/0", "r_frame_rate": "25/1"}, 25.0),
            ({"avg_frame_rate": "0/0", "r_frame_rate": "0/0", "nb_frames": "60", "duration": "2.0"}, 30.0),
            ({"avg_frame_rate": "0/0", "r_frame_rate": "0/0", "nb_frames": "0", "duration": "2.0"}, None),
            ({"avg_frame_rate": "24", "r_frame_rate": "x/1"}, None),
            ({"avg_frame_rate": "24/0", "r_frame_rate": "-24/1"}, None),
        ],
        ids=["declared", "frames-over-duration", "no-frames", "malformed", "zero-or-negative"],
    )
    def test_falls_back_through_each_frame_rate_reading(
        self, clip: Path, monkeypatch: pytest.MonkeyPatch, stream: dict[str, Any], expected_fps: float | None
    ) -> None:
        self._answer(monkeypatch, {"format": {}, "streams": [{"width": 640, "height": 480, **stream}]})
        probe = local_video_probe(str(clip))
        assert probe is not None
        assert probe.fps == expected_fps

    def test_reads_no_frame_rate_without_a_video_stream(self, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._answer(monkeypatch, {"format": {"duration": "3.0"}, "streams": []})
        probe = local_video_probe(str(clip))
        assert probe is not None
        assert (probe.info, probe.fps, probe.nominal_fps, probe.container_seconds) == (None, None, None, 3.0)

    def test_ignores_a_duration_that_is_not_a_number(self, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._answer(monkeypatch, {"format": {"duration": ["3.0"]}, "streams": [{"duration": "N/A"}]})
        probe = local_video_probe(str(clip))
        assert probe is not None
        assert (probe.seconds, probe.container_seconds) == (None, None)

    def _answer(self, monkeypatch: pytest.MonkeyPatch, output: dict[str, Any]) -> list[list[str]]:
        runs: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> SimpleNamespace:
            runs.append(cmd)
            return SimpleNamespace(stdout=json.dumps(output))

        monkeypatch.setattr(input_checks, "_installed_ffprobe", lambda: "/usr/bin/ffprobe")
        monkeypatch.setattr(input_checks.subprocess, "run", fake_run)
        return runs

    def test_reads_the_frame_count_a_file_records(self, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._answer(
            monkeypatch,
            {
                "format": {"duration": "3.0"},
                "streams": [{"avg_frame_rate": "24/1", "duration": "3.0", "nb_frames": "72"}],
            },
        )
        probe = local_video_probe(str(clip))
        assert probe is not None
        assert probe.frames == 72
        assert (probe.fewest_frames, probe.most_frames) == (72, 72)

    def test_estimates_frames_when_the_file_records_none(self, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # An MKV records no frame count: 3 s at 24 fps holds 72 frames.
        self._answer(
            monkeypatch,
            {
                "format": {"format_name": "matroska,webm", "duration": "3.0"},
                "streams": [{"avg_frame_rate": "24/1", "nb_frames": "N/A", "tags": {"DURATION": "00:00:03.000000000"}}],
            },
        )
        probe = local_video_probe(str(clip))
        assert probe is not None
        assert probe.frames is None
        assert (probe.fewest_frames, probe.most_frames) == (72, 72)

    def test_estimates_the_fewest_and_most_frames_from_every_reading(self) -> None:
        # 3 s of video in a 4 s container at 24 fps average, 30 fps declared.
        probe = VideoProbe(info=None, seconds=3.0, container_seconds=4.0, fps=24.0, nominal_fps=30.0)
        assert (probe.fewest_frames, probe.most_frames) == (72, 120)

    def test_reads_no_frames_without_a_duration_or_rate(self) -> None:
        assert VideoProbe(info=None, seconds=None, fps=24.0).fewest_frames is None
        assert VideoProbe(info=None, seconds=3.0).most_frames is None

    def test_reads_size_and_duration_in_one_ffprobe_run(self, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        runs = self._answer(
            monkeypatch,
            {
                "format": {"duration": "9.5"},
                "streams": [{"width": 1920, "height": 1080, "avg_frame_rate": "30000/1001"}],
            },
        )
        probe = local_video_probe(str(clip))
        assert probe is not None
        assert probe.info == ImageInfo(1920, 1080, None)
        assert probe.seconds == 9.5
        assert probe.fps == pytest.approx(29.97, abs=0.01)
        assert len(runs) == 1
        assert "-show_format" in runs[0]
        assert "-show_streams" in runs[0]

    def test_takes_the_duration_from_the_video_stream_not_the_container(
        self, clip: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A 10 s video with a 31 s audio track: the container reports the longer audio.
        self._answer(
            monkeypatch,
            {"format": {"duration": "31.0"}, "streams": [{"width": 1280, "height": 720, "duration": "10.0"}]},
        )
        probe = local_video_probe(str(clip))
        assert probe is not None
        assert probe.seconds == 10.0

    def test_falls_back_to_the_container_duration(self, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._answer(monkeypatch, {"format": {"duration": "7.5"}, "streams": [{"width": 1280, "height": 720}]})
        probe = local_video_probe(str(clip))
        assert probe is not None
        assert probe.seconds == 7.5

    def test_reports_the_part_ffprobe_gave_when_the_other_is_missing(
        self, clip: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._answer(monkeypatch, {"format": {"duration": "4.0"}, "streams": []})
        assert local_video_probe(str(clip)) == VideoProbe(info=None, seconds=4.0, fps=None, container_seconds=4.0)

    def test_skips_when_ffprobe_is_not_installed(self, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(input_checks, "_installed_ffprobe", lambda: None)
        assert local_video_probe(str(clip)) is None

    def test_skips_a_remote_video(self) -> None:
        assert local_video_probe("https://example.com/clip.mp4") is None

    @pytest.mark.parametrize(
        ("stream", "container", "shortest", "longest"),
        [(10.0, 31.0, 10.0, 31.0), (None, 7.5, 7.5, 7.5), (None, None, None, None)],
        ids=["both", "container-only", "neither"],
    )
    def test_reports_the_shorter_and_longer_duration(
        self, stream: float | None, container: float | None, shortest: float | None, longest: float | None
    ) -> None:
        probe = VideoProbe(info=None, seconds=stream, container_seconds=container)
        assert probe.shortest_seconds == shortest
        assert probe.longest_seconds == longest


class TestLocalVideoInfo:
    @pytest.fixture
    def clip(self, tmp_path: Path) -> Path:
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"video")
        return path

    def _answer(self, monkeypatch: pytest.MonkeyPatch, stream: dict[str, Any]) -> None:
        monkeypatch.setattr(input_checks, "_installed_ffprobe", lambda: "/usr/bin/ffprobe")
        monkeypatch.setattr(
            input_checks.subprocess,
            "run",
            lambda *_a, **_k: SimpleNamespace(stdout=json.dumps({"streams": [stream]})),
        )

    def test_reads_the_frame_size(self, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._answer(monkeypatch, {"width": 1920, "height": 1080})
        assert _probed_frame(str(clip)) == ImageInfo(1920, 1080, None)

    def test_treats_a_0x0_frame_size_as_unread(self, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._answer(monkeypatch, {"width": 0, "height": 0})
        assert _probed_frame(str(clip)) is None

    def test_keeps_the_stored_size_when_the_rotation_is_not_a_number(
        self, clip: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._answer(monkeypatch, {"width": 1920, "height": 1080, "tags": {"rotate": "sideways"}})
        assert _probed_frame(str(clip)) == ImageInfo(1920, 1080, None)

    @pytest.mark.parametrize(
        "stream",
        [
            {"width": 1920, "height": 1080, "tags": {"rotate": "90"}},
            {"width": 1920, "height": 1080, "side_data_list": [{"rotation": -90}]},
        ],
        ids=["rotate-tag", "display-matrix"],
    )
    def test_reports_a_rotated_clip_as_displayed(
        self, clip: Path, monkeypatch: pytest.MonkeyPatch, stream: dict[str, Any]
    ) -> None:
        self._answer(monkeypatch, stream)
        assert _probed_frame(str(clip)) == ImageInfo(1080, 1920, None)

    def test_skips_when_ffprobe_is_not_installed(self, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(input_checks, "_installed_ffprobe", lambda: None)
        assert _probed_frame(str(clip)) is None

    def test_skips_a_remote_video(self) -> None:
        assert _probed_frame("https://example.com/clip.mp4") is None


class TestInstalledFfprobe:
    @pytest.fixture(autouse=True)
    def no_ffmpeg_download(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fetch(*_args: Any, **_kwargs: Any) -> None:
            pytest.fail("a pre-run input check must never fetch ffmpeg")

        monkeypatch.setattr(input_checks.static_ffmpeg_run, "get_or_fetch_platform_executables_else_raise", fetch)

    def _platform_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exe_name: str = "ffprobe") -> Path:
        monkeypatch.setattr(input_checks.static_ffmpeg_run, "get_platform_dir", lambda: str(tmp_path))
        (tmp_path / exe_name).write_bytes(b"")
        return tmp_path

    @pytest.mark.parametrize(("platform", "exe_name"), [("linux", "ffprobe"), ("win32", "ffprobe.exe")])
    def test_uses_a_finished_static_ffmpeg_install(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, exe_name: str
    ) -> None:
        monkeypatch.setattr(input_checks.sys, "platform", platform)
        platform_dir = self._platform_dir(tmp_path, monkeypatch, exe_name)
        (platform_dir / "installed.crumb").write_text("installed")
        assert input_checks._installed_ffprobe() == str(platform_dir / exe_name)

    def test_ignores_an_unfinished_static_ffmpeg_install(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._platform_dir(tmp_path, monkeypatch)
        monkeypatch.setattr(input_checks.shutil, "which", lambda _name: None)
        assert input_checks._installed_ffprobe() is None

    def test_falls_back_to_ffprobe_on_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._platform_dir(tmp_path, monkeypatch)
        monkeypatch.setattr(input_checks.shutil, "which", lambda _name: "/opt/bin/ffprobe")
        assert input_checks._installed_ffprobe() == "/opt/bin/ffprobe"

    def test_falls_back_to_path_when_static_ffmpeg_has_no_directory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def no_dir() -> str:
            msg = "Read-only file system"
            raise OSError(msg)

        monkeypatch.setattr(input_checks.static_ffmpeg_run, "get_platform_dir", no_dir)
        monkeypatch.setattr(input_checks.shutil, "which", lambda _name: "/opt/bin/ffprobe")
        assert input_checks._installed_ffprobe() == "/opt/bin/ffprobe"

    def test_falls_back_to_path_when_a_finished_install_lacks_ffprobe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(input_checks.static_ffmpeg_run, "get_platform_dir", lambda: str(tmp_path))
        (tmp_path / "installed.crumb").write_text("installed")
        monkeypatch.setattr(input_checks.shutil, "which", lambda _name: "/opt/bin/ffprobe")
        assert input_checks._installed_ffprobe() == "/opt/bin/ffprobe"


class TestCheckCount:
    def test_allows_the_maximum(self) -> None:
        assert check_count(3, maximum=3, parameter="reference images") is None

    def test_rejects_more_than_the_maximum(self) -> None:
        error = check_count(5, maximum=3, parameter="reference images", noun="images", docs_url=DOCS)
        assert isinstance(error, NodeError)
        assert str(error) == "5 images are connected to 'reference images' and the limit is 3. Remove 2."
        assert error.links is not None
        assert error.links[0].url == DOCS


class TestCheckSize:
    def test_allows_the_maximum(self) -> None:
        assert check_size(7 * 1024 * 1024, maximum_bytes=7 * 1024 * 1024, subject=SUBJECT) is None

    def test_refuses_exactly_the_maximum_when_strict(self) -> None:
        error = check_size(30 * 1024 * 1024, maximum_bytes=30 * 1024 * 1024, subject=SUBJECT, strict=True)
        assert str(error) == f"{SUBJECT} is exactly 30 MB and it must be under 30 MB. Use a smaller file."

    def test_reports_a_file_at_a_decimal_strict_limit_as_exactly_it(self) -> None:
        # Providers writing "less than 30 MB" often mean decimal megabytes.
        error = check_size(30_000_000, maximum_bytes=30_000_000, subject=SUBJECT, strict=True)
        assert str(error) == f"{SUBJECT} is exactly 28.6102 MB and it must be under 28.6102 MB. Use a smaller file."

    def test_allows_just_under_the_maximum_when_strict(self) -> None:
        assert check_size(30 * 1024 * 1024 - 1, maximum_bytes=30 * 1024 * 1024, subject=SUBJECT, strict=True) is None

    def test_reports_a_larger_file_under_a_strict_limit(self) -> None:
        error = check_size(31 * 1024 * 1024, maximum_bytes=30 * 1024 * 1024, subject=SUBJECT, strict=True)
        assert str(error) == f"{SUBJECT} is 31.0 MB and it must be under 30 MB. Use a smaller file."

    def test_skips_an_unknown_size(self) -> None:
        assert check_size(None, maximum_bytes=1, subject=SUBJECT) is None

    def test_rejects_a_larger_file(self) -> None:
        error = check_size(int(8.5 * 1024 * 1024), maximum_bytes=7 * 1024 * 1024, subject=SUBJECT)
        assert error is not None
        assert str(error) == f"{SUBJECT} is 8.5 MB and the limit is 7 MB. Use a smaller file."
        assert not error.links

    def test_reports_a_size_just_over_the_limit_as_over_it(self) -> None:
        error = check_size(7 * 1024 * 1024 + 1024, maximum_bytes=7 * 1024 * 1024, subject=SUBJECT)
        assert str(error).startswith(f"{SUBJECT} is 7.001 MB and the limit is 7 MB.")

    def test_reports_a_size_over_a_non_round_limit_as_over_it(self) -> None:
        error = check_size(10_000_001, maximum_bytes=10_000_000, subject=SUBJECT)
        assert str(error).startswith(f"{SUBJECT} is 9.54 MB and the limit is 9.53674 MB.")

    def test_never_shows_the_size_and_limit_as_the_same_number(self) -> None:
        error = check_size(200_000_001, maximum_bytes=200_000_000, subject=SUBJECT)
        assert str(error).startswith(f"{SUBJECT} is 190.735 MB and the limit is 190.734 MB.")

    def test_uses_the_given_fix(self) -> None:
        error = check_size(2, maximum_bytes=1, subject=SUBJECT, fix="Turn on 'Auto Image Resize'.")
        assert error is not None
        assert str(error).endswith("Turn on 'Auto Image Resize'.")


class TestCheckDimensions:
    def test_allows_an_image_within_every_limit(self) -> None:
        info = ImageInfo(1280, 720, "image/png")
        assert check_dimensions(info, subject=SUBJECT, min_side=200, max_side=4096, max_megapixels=16) is None

    def test_skips_an_unknown_image(self) -> None:
        assert check_dimensions(None, subject=SUBJECT, min_side=10_000) is None

    def test_rejects_a_short_side(self) -> None:
        error = check_dimensions(ImageInfo(1000, 150, None), subject=SUBJECT, min_side=200)
        assert str(error) == f"{SUBJECT} is 1000x150 and each side must be at least 200 pixels. Use a larger one."

    def test_rejects_a_long_side(self) -> None:
        error = check_dimensions(ImageInfo(300, 5000, None), subject=SUBJECT, max_side=4096)
        assert str(error) == f"{SUBJECT} is 300x5000 and each side must be at most 4096 pixels. Resize it smaller."

    def test_rejects_too_many_pixels(self) -> None:
        error = check_dimensions(ImageInfo(4000, 4000, None), subject=SUBJECT, max_megapixels=8)
        assert str(error) == (
            f"{SUBJECT} is 4000x4000 (16.0 megapixels) and the limit is 8 megapixels. Resize it smaller."
        )

    def test_reports_megapixels_just_over_the_limit_as_over_them(self) -> None:
        error = check_dimensions(ImageInfo(2829, 2829, None), subject=SUBJECT, max_megapixels=8)
        assert "(8.003 megapixels)" in str(error)

    def test_allows_exactly_the_minimum_area(self) -> None:
        assert check_dimensions(ImageInfo(614, 664, None), subject=SUBJECT, min_megapixels=0.407696) is None

    def test_rejects_too_few_pixels(self) -> None:
        error = check_dimensions(ImageInfo(640, 480, None), subject=SUBJECT, min_megapixels=0.407696)
        assert str(error) == (
            f"{SUBJECT} is 640x480 (0.3 megapixels) and the minimum is 0.407696 megapixels. Use a larger one."
        )

    def test_reports_megapixels_just_under_the_minimum_as_under_it(self) -> None:
        error = check_dimensions(ImageInfo(1999, 1000, None), subject=SUBJECT, min_megapixels=2)
        assert "(1.999 megapixels) and the minimum is 2 megapixels" in str(error)


class TestCheckAspectRatioRange:
    def test_refuses_bounds_given_widest_first(self) -> None:
        with pytest.raises(ValueError, match="pass the tall bound first"):
            check_aspect_ratio_range(None, subject=SUBJECT, widest="9:16", tallest="16:9")

    @pytest.mark.parametrize(("width", "height"), [(300, 900), (900, 300), (1000, 1000), (1920, 1080)])
    def test_allows_a_shape_in_range_including_the_ends(self, width: int, height: int) -> None:
        info = ImageInfo(width, height, None)
        assert check_aspect_ratio_range(info, subject=SUBJECT, widest="3:1", tallest="1:3") is None

    def test_rejects_a_shape_that_is_too_wide(self) -> None:
        error = check_aspect_ratio_range(ImageInfo(1000, 300, None), subject=SUBJECT, widest="3:1", tallest="1:3")
        assert str(error) == (
            f"{SUBJECT} is 1000x300, which is too wide. The aspect ratio must be between 1:3 and 3:1. Crop it."
        )

    def test_rejects_a_shape_that_is_too_tall(self) -> None:
        error = check_aspect_ratio_range(ImageInfo(250, 1000, None), subject=SUBJECT, widest="3:1", tallest="1:3")
        assert str(error) == (
            f"{SUBJECT} is 250x1000, which is too tall. The aspect ratio must be between 1:3 and 3:1. Crop it."
        )

    def test_names_the_bound_that_failed_for_a_portrait_only_range(self) -> None:
        error = check_aspect_ratio_range(ImageInfo(1000, 1000, None), subject=SUBJECT, widest="3:4", tallest="9:16")
        assert "which is too wide" in str(error)

    def test_skips_an_unknown_image(self) -> None:
        assert check_aspect_ratio_range(None, subject=SUBJECT, widest="3:1", tallest="1:3") is None


class TestCheckFrameCount:
    def test_refuses_too_many_frames(self) -> None:
        error = check_frame_count(9001, subject=SUBJECT, maximum=9000, docs_url=DOCS)
        assert str(error) == (
            f"{SUBJECT} has 9,001 frames and the limit is 9,000. Use a Trim Video node to shorten it, or lower its "
            "frame rate."
        )
        assert error is not None
        assert error.links
        assert error.links[0].url == DOCS

    def test_refuses_too_few_frames(self) -> None:
        error = check_frame_count(72, subject=SUBJECT, minimum=73)
        assert str(error) == f"{SUBJECT} has 72 frames and the minimum is 73. Use a longer clip."

    def test_accepts_counts_on_the_limits_and_skips_an_unread_count(self) -> None:
        assert check_frame_count(73, subject=SUBJECT, minimum=73, maximum=181) is None
        assert check_frame_count(181, subject=SUBJECT, minimum=73, maximum=181) is None
        assert check_frame_count(None, subject=SUBJECT, minimum=73) is None


class TestCheckFrameRate:
    @pytest.mark.parametrize("fps", [24.0, 23.976, 30.0, 59.94, 60.0])
    def test_allows_a_frame_rate_in_range_including_ntsc(self, fps: float) -> None:
        assert check_frame_rate(fps, subject=SUBJECT, minimum=24, maximum=60) is None

    def test_skips_an_unknown_frame_rate(self) -> None:
        assert check_frame_rate(None, subject=SUBJECT, minimum=24) is None

    def test_rejects_a_low_frame_rate(self) -> None:
        error = check_frame_rate(15.0, subject=SUBJECT, minimum=24, maximum=60, docs_url=DOCS)
        assert str(error) == f"{SUBJECT} is 15 fps and the minimum is 24 fps. Re-export it at a higher frame rate."
        assert error is not None
        assert error.links

    def test_prints_a_very_high_frame_rate_in_full(self) -> None:
        error = check_frame_rate(1000.0, subject=SUBJECT, maximum=60)
        assert str(error).startswith(f"{SUBJECT} is 1000 fps and the maximum is 60 fps.")

    def test_never_prints_a_frame_rate_as_equal_to_the_limit(self) -> None:
        error = check_frame_rate(23.97, subject=SUBJECT, minimum=24, tolerance=0.001)
        assert str(error).startswith(f"{SUBJECT} is 23.97 fps and the minimum is 24 fps.")

    def test_rejects_a_high_frame_rate(self) -> None:
        error = check_frame_rate(120.0, subject=SUBJECT, minimum=24, maximum=60)
        assert str(error) == f"{SUBJECT} is 120 fps and the maximum is 60 fps. Re-export it at a lower frame rate."


class TestCheckAspectRatioExact:
    RATIOS = ("16:9", "9:16")

    @pytest.mark.parametrize(
        ("width", "height"),
        [
            (1920, 1080),
            (3840, 2160),
            (1280, 720),
            (854, 480),
            (1366, 768),
            (426, 240),
            (1080, 1920),
            (480, 854),
            # Portrait frames scaled with the width rounded, as ffmpeg's scale=-2:720 does.
            (406, 720),
            (270, 480),
        ],
    )
    def test_allows_a_listed_ratio_including_sizes_rounded_to_whole_pixels(self, width: int, height: int) -> None:
        assert check_aspect_ratio_exact(ImageInfo(width, height, None), subject=SUBJECT, ratios=self.RATIOS) is None

    @pytest.mark.parametrize(("width", "height"), [(1440, 1080), (1000, 1000), (1920, 1088), (1920, 1000)])
    def test_refuses_another_ratio(self, width: int, height: int) -> None:
        error = check_aspect_ratio_exact(ImageInfo(width, height, None), subject=SUBJECT, ratios=self.RATIOS)
        assert str(error) == f"{SUBJECT} is {width}x{height}, and its aspect ratio must be 16:9 or 9:16. Crop it."

    def test_skips_an_unread_size(self) -> None:
        assert check_aspect_ratio_exact(None, subject=SUBJECT, ratios=self.RATIOS) is None

    def test_refuses_ratios_given_as_one_string(self) -> None:
        with pytest.raises(TypeError, match="pass a tuple"):
            check_aspect_ratio_exact(None, subject=SUBJECT, ratios="16:9")

    @pytest.mark.parametrize("ratios", [(), ("16x9",)])
    def test_refuses_no_ratios_or_a_bad_one_before_reading_anything(self, ratios: tuple[str, ...]) -> None:
        with pytest.raises(ValueError, match="Attempted to check"):
            check_aspect_ratio_exact(None, subject=SUBJECT, ratios=ratios)

    @pytest.mark.parametrize("ratio", ["1:inf", "inf:1", "nan:1"])
    def test_refuses_a_ratio_that_is_not_finite(self, ratio: str) -> None:
        with pytest.raises(ValueError, match="is not a ratio such as '16:9'"):
            check_aspect_ratio_exact(None, subject=SUBJECT, ratios=(ratio,))


class TestVideoEncoding:
    @pytest.mark.parametrize(
        ("demuxer", "brand", "codec", "expected"),
        [
            ("mov,mp4,m4a,3gp,3g2,mj2", "isom", "h264", "video/mp4"),
            ("mov,mp4,m4a,3gp,3g2,mj2", "mp42", "h264", "video/mp4"),
            ("mov,mp4,m4a,3gp,3g2,mj2", "qt  ", "h264", "video/quicktime"),
            ("mov,mp4,m4a,3gp,3g2,mj2", None, "h264", "video/quicktime"),
            ("mov,mp4,m4a,3gp,3g2,mj2", "3gp6", "h264", "video/3gpp"),
            ("matroska,webm", None, "vp9", "video/webm"),
            ("matroska,webm", None, "h264", "video/x-matroska"),
            ("avi", None, "mpeg4", "video/x-msvideo"),
            # A container ffprobe named but this module doesn't map is named after it, so it can be refused.
            ("something_new", None, "h264", "video/x-ffprobe-something_new"),
            # HEIC and AVIF photos are ISO media files; they read as images, so a video check refuses them.
            ("mov,mp4,m4a,3gp,3g2,mj2", "heic", "hevc", "image/heif"),
            ("mov,mp4,m4a,3gp,3g2,mj2", "mif1", "hevc", "image/heif"),
            ("mov,mp4,m4a,3gp,3g2,mj2", "avif", "av1", "image/avif"),
            # A still image ffprobe opens with an image demuxer reads as that image format.
            ("png_pipe", None, "png", "image/png"),
        ],
    )
    def test_reads_the_container_from_the_file_not_its_name(
        self, demuxer: str, brand: str | None, codec: str, expected: str | None
    ) -> None:
        tags = {} if brand is None else {"major_brand": brand}
        data = {"format": {"format_name": demuxer, "tags": tags}, "streams": [{"codec_name": codec}]}
        assert input_checks._video_mime_type(data) == expected

    def test_refuses_a_codec_by_the_name_artists_know(self) -> None:
        error = check_video_codec("hevc", subject=SUBJECT, allowed=("h264",))
        assert str(error) == f"{SUBJECT} is encoded as H.265. Re-encode it as H.264."

    def test_allows_a_listed_codec_and_skips_an_unread_one(self) -> None:
        assert check_video_codec("h264", subject=SUBJECT, allowed=("h264", "hevc")) is None
        assert check_video_codec(None, subject=SUBJECT, allowed=("h264",)) is None

    def test_refuses_codecs_given_as_one_string(self) -> None:
        with pytest.raises(TypeError, match="pass a tuple"):
            check_video_codec("h264", subject=SUBJECT, allowed="h264")

    @pytest.mark.parametrize(
        ("header", "codec", "expected"),
        [
            (b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\x82\x84webm", "vp9", "video/webm"),
            # An MKV can carry a WebM codec; its header still says matroska.
            (b"\x1a\x45\xdf\xa3\xa3\x42\x86\x81\x01\x42\x82\x88matroska", "vp9", "video/x-matroska"),
            (b"\x1a\x45\xdf\xa3\xa3\x42\x86\x81\x01\x42\x82\x88matroska", "h264", "video/x-matroska"),
        ],
    )
    def test_tells_webm_from_mkv_by_the_files_doctype(
        self, tmp_path: Path, header: bytes, codec: str, expected: str
    ) -> None:
        path = tmp_path / "clip.mkv"
        path.write_bytes(header + b"\x00" * 32)
        data: dict[str, Any] = {"format": {"format_name": "matroska,webm"}, "streams": [{"codec_name": codec}]}
        input_checks._record_matroska_doctype(data, path)
        assert input_checks._video_mime_type(data) == expected

    def test_falls_back_to_the_codec_when_the_doctype_is_unread(self) -> None:
        data = {"format": {"format_name": "matroska,webm"}, "streams": [{"codec_name": "vp9"}]}
        assert input_checks._video_mime_type(data) == "video/webm"

    @pytest.mark.parametrize(
        "header",
        [
            b"\x1a\x45\xdf\xa3\x42\x82",  # the DocType ID ends the header
            b"\x1a\x45\xdf\xa3\x42\x82\x04webm",  # a multi-byte size
            b"\x1a\x45\xdf\xa3\x42\x82\x88web",  # a size past the header's end
        ],
        ids=["no-size", "long-size", "truncated"],
    )
    def test_reads_no_doctype_from_a_header_it_cannot_parse(self, header: bytes) -> None:
        assert input_checks._matroska_doctype(header) is None

    def test_leaves_the_doctype_unread_when_the_file_is_gone(self, tmp_path: Path) -> None:
        data: dict[str, Any] = {"format": {"format_name": "matroska,webm"}, "streams": [{"codec_name": "vp9"}]}
        input_checks._record_matroska_doctype(data, tmp_path / "deleted-since.mkv")
        assert input_checks._DOCTYPE_KEY not in data["format"]
        assert input_checks._video_mime_type(data) == "video/webm"

    def test_reads_a_3g2_brand_as_3gpp2(self) -> None:
        data = {"format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "tags": {"major_brand": "3g2a"}}}
        assert input_checks._video_mime_type(data) == "video/3gpp2"

    @pytest.mark.parametrize(
        ("codec", "allowed"),
        [
            ("prores", ("ProRes",)),
            ("hevc", ("HEVC",)),
            ("hevc", ("h265",)),
            ("h264", ("AVC",)),
            ("h264", ("H264",)),
            ("h264", ("H.264",)),
        ],
    )
    def test_matches_a_codec_however_a_provider_writes_it(self, codec: str, allowed: tuple[str, ...]) -> None:
        assert check_video_codec(codec, subject=SUBJECT, allowed=allowed) is None

    def test_shows_provider_written_codecs_by_their_known_names(self) -> None:
        error = check_video_codec("vp9", subject=SUBJECT, allowed=("ProRes", "HEVC"))
        assert str(error) == f"{SUBJECT} is encoded as VP9. Re-encode it as ProRes or H.265."

    @pytest.mark.parametrize(
        ("demuxer", "expected"), [("mxf", "application/mxf"), ("dv", "video/dv"), ("ogg", "video/ogg")]
    )
    def test_names_more_video_containers(self, demuxer: str, expected: str) -> None:
        data = {"format": {"format_name": demuxer}, "streams": [{"codec_name": "mpeg2video"}]}
        assert input_checks._video_mime_type(data) == expected

    def test_refuses_a_named_container_the_provider_doesnt_list(self) -> None:
        error = check_mime_type("application/mxf", subject=SUBJECT, allowed=("video/mp4", "video/quicktime"))
        assert str(error) == f"{SUBJECT} is in MXF format. Convert it to MP4 or MOV."

    def test_reads_a_matroska_streams_duration_from_its_tag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clip = tmp_path / "clip.mkv"
        clip.write_bytes(b"video")
        answer = json.dumps(
            {
                "streams": [
                    {"codec_name": "h264", "width": 320, "height": 240, "tags": {"DURATION": "00:00:03.023000000"}}
                ],
                "format": {"format_name": "matroska,webm", "duration": "10.0"},
            }
        )
        monkeypatch.setattr(input_checks, "_installed_ffprobe", lambda: "/usr/bin/ffprobe")
        monkeypatch.setattr(input_checks.subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout=answer))
        probe = local_video_probe(str(clip))
        assert probe is not None
        assert probe.seconds == pytest.approx(3.023)
        assert probe.container_seconds == 10.0

    def test_reads_a_duration_tag_named_for_its_language(self) -> None:
        assert input_checks._stream_seconds({"tags": {"DURATION-eng": "00:00:03.500000000"}}) == pytest.approx(3.5)

    def test_names_an_unmapped_container_in_a_refusal(self) -> None:
        error = check_mime_type("video/x-ffprobe-something_new", subject=SUBJECT, allowed=("video/mp4",))
        assert str(error) == f"{SUBJECT} is in SOMETHING_NEW format. Convert it to MP4."

    def test_refuses_a_still_image_by_its_image_format(self) -> None:
        error = check_mime_type("image/png", subject=SUBJECT, allowed=("video/mp4",))
        assert str(error) == f"{SUBJECT} is in PNG format. Convert it to MP4."

    @pytest.mark.parametrize("tag", ["N/A", "", "1:2", "bad", "-00:00:01.0", "00:00:inf", "00:00:nan"])
    def test_ignores_a_duration_tag_that_isnt_a_clock_time(self, tag: str) -> None:
        assert input_checks._stream_seconds({"tags": {"DURATION": tag}}) is None

    @pytest.mark.parametrize(
        ("width", "height", "sar", "displayed"),
        [(1440, 1080, "4:3", (1920, 1080)), (720, 480, "32:27", (853, 480))],
    )
    def test_reads_an_anamorphic_frames_displayed_shape(
        self, width: int, height: int, sar: str, displayed: tuple[int, int]
    ) -> None:
        data = {"streams": [{"width": width, "height": height, "sample_aspect_ratio": sar}]}
        assert input_checks._display_frame_size(data) == ImageInfo(*displayed, None)

    @pytest.mark.parametrize("sar", ["1:1", "0:1", "N/A", None])
    def test_has_no_displayed_shape_for_square_or_unknown_pixels(self, sar: str | None) -> None:
        data = {"streams": [{"width": 1920, "height": 1080, "sample_aspect_ratio": sar}]}
        assert input_checks._display_frame_size(data) is None

    def test_scales_the_upright_height_of_a_rotated_anamorphic_frame(self) -> None:
        # Turned upright, 1440x1080 is 1080x1440, and its 4:3 pixels stretch the 1440 side.
        data = {"streams": [{"width": 1440, "height": 1080, "sample_aspect_ratio": "4:3", "tags": {"rotate": "90"}}]}
        assert input_checks._display_frame_size(data) == ImageInfo(1080, 1920, None)


class TestAudio:
    @pytest.fixture
    def clip(self, tmp_path: Path) -> Path:
        path = tmp_path / "clip.wav"
        path.write_bytes(b"audio")
        return path

    def _answer(self, monkeypatch: pytest.MonkeyPatch, stream: dict[str, Any], format_info: dict[str, Any]) -> None:
        """Pretend ffprobe is installed and answers with one audio stream and this container."""
        monkeypatch.setattr(input_checks, "_installed_ffprobe", lambda: "/usr/bin/ffprobe")
        answer = json.dumps({"streams": [stream], "format": format_info})
        monkeypatch.setattr(input_checks.subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout=answer))

    @pytest.mark.parametrize(
        ("demuxer", "brand", "expected"),
        [
            ("mp3", None, "audio/mpeg"),
            ("wav", None, "audio/wav"),
            ("flac", None, "audio/flac"),
            ("ogg", None, "audio/ogg"),
            ("mov,mp4,m4a,3gp,3g2,mj2", "M4A ", "audio/mp4"),
            ("mov,mp4,m4a,3gp,3g2,mj2", "isom", "audio/mp4"),
            ("mov,mp4,m4a,3gp,3g2,mj2", "qt  ", "video/quicktime"),
            ("matroska,webm", None, "audio/x-matroska"),
            # A DVD's MPEG program stream holds MP2 audio, but it isn't an MP3 file.
            ("mpeg", None, "video/mpeg"),
            ("mov,mp4,m4a,3gp,3g2,mj2", "heic", "image/heif"),
            ("mov,mp4,m4a,3gp,3g2,mj2", "avif", "image/avif"),
        ],
    )
    def test_reads_the_container_from_the_file(
        self, clip: Path, monkeypatch: pytest.MonkeyPatch, demuxer: str, brand: str | None, expected: str
    ) -> None:
        tags = {} if brand is None else {"major_brand": brand}
        self._answer(
            monkeypatch,
            {"codec_name": "mp3", "duration": "3.0"},
            {"format_name": demuxer, "duration": "3.0", "tags": tags},
        )
        probe = local_audio_probe(str(clip))
        assert probe is not None
        assert probe.mime_type == expected

    def test_refuses_an_mpeg_program_stream_where_mp3_is_asked(self) -> None:
        error = check_mime_type("video/mpeg", subject=SUBJECT, allowed=("audio/mpeg",))
        assert str(error) == f"{SUBJECT} is in MPEG format. Convert it to MP3."

    def test_reads_an_aac_profile_and_drops_ffprobes_unknown(self, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._answer(
            monkeypatch,
            {"codec_name": "aac", "profile": "HE-AAC", "duration": "4.0"},
            {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "4.0", "tags": {"major_brand": "M4A "}},
        )
        assert local_audio_probe(str(clip)) == AudioProbe(
            mime_type="audio/mp4", codec="aac", profile="HE-AAC", seconds=4.0, container_seconds=4.0
        )
        self._answer(monkeypatch, {"codec_name": "mp3", "profile": "unknown"}, {"format_name": "mp3"})
        probe = local_audio_probe(str(clip))
        assert probe is not None
        assert probe.profile is None

    def test_reads_nothing_from_a_file_with_no_audio(self, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(input_checks, "_installed_ffprobe", lambda: "/usr/bin/ffprobe")
        answer = json.dumps({"streams": [], "format": {"format_name": "mp4"}})
        monkeypatch.setattr(input_checks.subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout=answer))
        assert local_audio_probe(str(clip)) is None

    @pytest.mark.parametrize("format_info", [None, {}, {"format_name": 3}], ids=["no-format", "no-name", "not-text"])
    def test_reads_no_container_when_ffprobe_names_none(
        self, clip: Path, monkeypatch: pytest.MonkeyPatch, format_info: Any
    ) -> None:
        monkeypatch.setattr(input_checks, "_installed_ffprobe", lambda: "/usr/bin/ffprobe")
        output: dict[str, Any] = {"streams": [{"codec_name": "mp3", "duration": "3.0"}]}
        if format_info is not None:
            output["format"] = format_info
        answer = json.dumps(output)
        monkeypatch.setattr(input_checks.subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout=answer))
        probe = local_audio_probe(str(clip))
        assert probe is not None
        assert (probe.mime_type, probe.codec, probe.seconds) == (None, "mp3", 3.0)

    def test_skips_audio_when_ffprobe_is_not_installed(self, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(input_checks, "_installed_ffprobe", lambda: None)
        assert local_audio_probe(str(clip)) is None

    @pytest.mark.parametrize(("brand", "expected"), [("3gp5", "audio/3gpp"), ("3g2a", "audio/3gpp2")])
    def test_reads_3gpp_audio_by_its_brand(
        self, clip: Path, monkeypatch: pytest.MonkeyPatch, brand: str, expected: str
    ) -> None:
        self._answer(
            monkeypatch,
            {"codec_name": "aac", "duration": "3.0"},
            {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "tags": {"major_brand": brand}},
        )
        probe = local_audio_probe(str(clip))
        assert probe is not None
        assert probe.mime_type == expected

    def test_refuses_codecs_given_as_one_string(self) -> None:
        with pytest.raises(TypeError, match="pass a tuple"):
            check_audio_codec("mp3", subject=SUBJECT, allowed="mp3")

    @pytest.mark.parametrize(
        ("codec", "expected"), [("opus", "audio/webm"), ("vorbis", "audio/webm"), ("aac", "audio/x-matroska")]
    )
    def test_tells_webm_from_mka_by_codec(
        self, clip: Path, monkeypatch: pytest.MonkeyPatch, codec: str, expected: str
    ) -> None:
        self._answer(monkeypatch, {"codec_name": codec, "duration": "3.0"}, {"format_name": "matroska,webm"})
        probe = local_audio_probe(str(clip))
        assert probe is not None
        assert probe.mime_type == expected

    def test_names_pcm_byte_order(self) -> None:
        error = check_audio_codec("pcm_s16be", subject=SUBJECT, allowed=("mp3", "pcm_s16le"))
        assert str(error) == (
            f"{SUBJECT} is encoded as 16-bit PCM (big-endian). Re-encode it as MP3 or 16-bit PCM (little-endian)."
        )

    def test_accepts_an_ogg_opus_file_listed_as_audio_opus(self) -> None:
        assert check_mime_type("audio/ogg", subject=SUBJECT, allowed=("audio/opus", "audio/mpeg")) is None

    @pytest.mark.parametrize("allowed", [("H.264", "H.265"), ("h264",), ("AVC", "H.264")])
    def test_accepts_a_codec_written_the_way_provider_docs_do(self, allowed: tuple[str, ...]) -> None:
        assert check_video_codec("h264", subject=SUBJECT, allowed=allowed) is None

    def test_lists_a_codec_once_when_the_provider_names_it_twice(self) -> None:
        error = check_video_codec("vp9", subject=SUBJECT, allowed=("hevc", "H.265"))
        assert str(error) == f"{SUBJECT} is encoded as VP9. Re-encode it as H.265."

    @pytest.mark.parametrize(("codec", "allowed"), [("opus", ("Opus",)), ("aac", ("AAC",)), ("vorbis", ("Vorbis",))])
    def test_matches_an_audio_codec_however_a_provider_writes_it(self, codec: str, allowed: tuple[str, ...]) -> None:
        assert check_audio_codec(codec, subject=SUBJECT, allowed=allowed) is None

    @pytest.mark.parametrize("brand", ["3gp4", "3gp6"])
    def test_names_3gp_audio_as_3gp(self, clip: Path, monkeypatch: pytest.MonkeyPatch, brand: str) -> None:
        self._answer(
            monkeypatch,
            {"codec_name": "aac", "duration": "3.0"},
            {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "tags": {"major_brand": brand}},
        )
        probe = local_audio_probe(str(clip))
        assert probe is not None
        assert probe.mime_type == "audio/3gpp"

    def test_reads_a_matroska_audio_tracks_own_length(self, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A 3 s audio track in a 10 s MKV: ffprobe gives the track's length only as a DURATION tag.
        self._answer(
            monkeypatch,
            {"codec_name": "aac", "tags": {"DURATION": "00:00:03.023000000"}},
            {"format_name": "matroska,webm", "duration": "10.0"},
        )
        probe = local_audio_probe(str(clip))
        assert probe is not None
        assert probe.shortest_seconds == pytest.approx(3.023)

    @pytest.mark.parametrize(
        ("demuxer", "expected"),
        [
            ("asf", "audio/x-ms-wma"),
            ("caf", "audio/x-caf"),
            ("au", "audio/basic"),
            ("ac3", "audio/ac3"),
            ("tta", "audio/x-ffprobe-tta"),
        ],
    )
    def test_names_more_audio_containers(
        self, clip: Path, monkeypatch: pytest.MonkeyPatch, demuxer: str, expected: str
    ) -> None:
        self._answer(monkeypatch, {"codec_name": "wmav2", "duration": "1.0"}, {"format_name": demuxer})
        probe = local_audio_probe(str(clip))
        assert probe is not None
        assert probe.mime_type == expected


class TestCodecNames:
    @pytest.mark.parametrize(
        ("codec", "written"),
        [
            ("h264", "h264"),
            ("h264", "H.264"),
            ("h264", "AVC"),
            ("hevc", "HEVC"),
            ("hevc", "H.265"),
            ("hevc", "h265"),
            ("ac3", "AC-3"),
            ("pcm_s16le", "pcm_s16le"),
            ("pcm_mulaw", "G.711 μ-law"),
        ],
    )
    def test_accepts_ffprobes_name_the_shown_name_or_an_alias(self, codec: str, written: str) -> None:
        assert check_audio_codec(codec, subject=SUBJECT, allowed=(written,)) is None

    @pytest.mark.parametrize("written", ["H.264 or H.265", "Apple ProRes", "PCM", "AAC-LC", "x264"])
    def test_raises_on_a_name_it_doesnt_know(self, written: str) -> None:
        with pytest.raises(ValueError, match="isn't a codec this module knows"):
            check_video_codec("h264", subject=SUBJECT, allowed=(written,))

    def test_names_a_codec_it_doesnt_list_by_ffprobes_name(self) -> None:
        error = check_video_codec("theora", subject=SUBJECT, allowed=("h264",))
        assert str(error) == f"{SUBJECT} is encoded as THEORA. Re-encode it as H.264."


class TestCheckMimeType:
    def test_accepts_an_mp4_videos_audio_for_an_audio_input(self) -> None:
        assert check_mime_type("audio/mp4", subject=SUBJECT, allowed=("video/mp4",), audio=True) is None

    def test_does_not_widen_for_a_non_audio_input(self) -> None:
        assert check_mime_type("audio/mp4", subject=SUBJECT, allowed=("video/mp4",)) is not None

    def test_allows_a_listed_type(self) -> None:
        assert check_mime_type("image/png", subject=SUBJECT, allowed=("image/jpeg", "image/png")) is None

    def test_skips_an_unknown_type(self) -> None:
        assert check_mime_type(None, subject=SUBJECT, allowed=("image/png",)) is None

    def test_rejects_an_unlisted_type(self) -> None:
        error = check_mime_type("image/webp", subject=SUBJECT, allowed=("image/jpeg", "image/png"))
        assert str(error) == f"{SUBJECT} is in WEBP format. Convert it to JPEG or PNG."

    @pytest.mark.parametrize(
        ("mime_type", "name"),
        [
            ("image/avif", "AVIF"),
            ("image/x-icon", "ICON"),
            ("image/vnd.adobe.photoshop", "PSD"),
            ("video/mp4", "MP4"),
            ("video/quicktime", "MOV"),
            ("video/x-matroska", "MKV"),
            ("video/x-msvideo", "AVI"),
        ],
        ids=["subtype", "strips-x-prefix", "known-name", "mp4", "mov", "mkv", "avi"],
    )
    def test_names_the_format_the_way_artists_know_it(self, mime_type: str, name: str) -> None:
        error = check_mime_type(mime_type, subject=SUBJECT, allowed=("image/jpeg", "image/png"))
        assert str(error) == f"{SUBJECT} is in {name} format. Convert it to JPEG or PNG."

    def test_refuses_allowed_formats_given_as_one_string(self) -> None:
        with pytest.raises(TypeError, match="pass a tuple"):
            check_mime_type("image/gif", subject=SUBJECT, allowed="image/png, image/jpeg")

    @pytest.mark.parametrize(
        ("mime_type", "allowed"),
        [
            ("audio/mpeg", ("audio/mp3",)),
            ("audio/wav", ("audio/x-wav", "audio/mpeg")),
            ("audio/wav", ("audio/wave",)),
            ("audio/mp4", ("audio/x-m4a",)),
            ("audio/flac", ("audio/x-flac",)),
            ("image/heif", ("image/heic",)),
        ],
    )
    def test_accepts_a_format_under_another_name_a_provider_writes(
        self, mime_type: str, allowed: tuple[str, ...]
    ) -> None:
        assert check_mime_type(mime_type, subject=SUBJECT, allowed=allowed) is None

    def test_still_refuses_a_different_format_under_an_alias(self) -> None:
        error = check_mime_type("audio/flac", subject=SUBJECT, allowed=("audio/mp3", "audio/x-wav"))
        assert str(error) == f"{SUBJECT} is in FLAC format. Convert it to MP3 or WAV."

    def test_lists_each_format_once_under_the_name_the_provider_wrote(self) -> None:
        assert (
            str(check_mime_type("image/gif", subject=SUBJECT, allowed=("image/jpeg", "image/heic", "image/heif")))
            == f"{SUBJECT} is in GIF format. Convert it to JPEG, HEIC or HEIF."
        )
        assert (
            str(check_mime_type("audio/flac", subject=SUBJECT, allowed=("audio/wav", "audio/x-wav", "audio/mp3")))
            == f"{SUBJECT} is in FLAC format. Convert it to WAV or MP3."
        )

    @pytest.mark.parametrize(
        ("mime_type", "allowed"),
        [
            ("image/jpeg", ("image/jpg", "image/png")),
            ("video/quicktime", ("video/mp4", "video/mov")),
            ("video/x-msvideo", ("video/avi",)),
            ("audio/ogg", ("audio/x-ogg",)),
            ("audio/aiff", ("audio/aif",)),
            ("audio/mp4", ("audio/mp4a-latm",)),
        ],
    )
    def test_accepts_more_provider_spellings(self, mime_type: str, allowed: tuple[str, ...]) -> None:
        assert check_mime_type(mime_type, subject=SUBJECT, allowed=allowed) is None

    def test_never_names_the_input_format_among_the_ones_it_asks_for(self) -> None:
        # An unlisted spelling that reads the same in a message still matches.
        assert check_mime_type("audio/x-made-up", subject=SUBJECT, allowed=("audio/x-made-up",)) is None
        assert check_mime_type("video/x-made-up", subject=SUBJECT, allowed=("video/made-up",)) is None

    @pytest.mark.parametrize(
        ("mime_type", "allowed"),
        [
            ("audio/mpeg", ("audio/MPEG",)),
            ("video/webm", ("video/WebM",)),
            ("video/quicktime", ("video/QuickTime",)),
            ("audio/wav", ("audio/X-WAV",)),
            ("video/x-matroska", ("video/matroska",)),
            ("video/x-matroska", ("video/mkv",)),
            ("audio/x-matroska", ("audio/matroska",)),
            ("audio/mpeg", ("audio/mpeg3",)),
        ],
    )
    def test_matches_any_capitalisation_and_registered_spelling(self, mime_type: str, allowed: tuple[str, ...]) -> None:
        assert check_mime_type(mime_type, subject=SUBJECT, allowed=allowed) is None

    @pytest.mark.parametrize(
        ("mime_type", "allowed"),
        [("audio/ogg", ("audio/ogg; codecs=opus",)), ("audio/webm", ("audio/webm;codecs=opus",))],
    )
    def test_ignores_mime_type_parameters(self, mime_type: str, allowed: tuple[str, ...]) -> None:
        assert check_mime_type(mime_type, subject=SUBJECT, allowed=allowed) is None

    def test_names_a_parameterised_type_without_its_parameters(self) -> None:
        error = check_mime_type("audio/flac", subject=SUBJECT, allowed=("audio/webm;codecs=opus",))
        assert str(error) == f"{SUBJECT} is in FLAC format. Convert it to WebM."

    def test_treats_an_empty_list_as_no_limit(self) -> None:
        assert check_mime_type("image/png", subject=SUBJECT, allowed=()) is None
        assert check_video_codec("h264", subject=SUBJECT, allowed=()) is None
        assert check_audio_codec("mp3", subject=SUBJECT, allowed=()) is None


class TestCheckDuration:
    def test_allows_a_duration_in_range(self) -> None:
        assert check_duration(5.0, subject="The video in 'Video'", minimum=1, maximum=8.7) is None

    def test_skips_an_unknown_duration(self) -> None:
        assert check_duration(None, subject="The video in 'Video'", maximum=1) is None

    def test_rejects_a_long_video(self) -> None:
        error = check_duration(12.345, subject="The video in 'Video'", maximum=8.7)
        assert str(error) == (
            "The video in 'Video' is 12.35 seconds long and the limit is 8.7. Use a Trim Video node to shorten it."
        )

    def test_rejects_a_short_video(self) -> None:
        error = check_duration(0.5, subject="The video in 'Video'", minimum=2)
        assert str(error) == "The video in 'Video' is 0.50 seconds long and the minimum is 2. Use a longer clip."

    def test_reports_a_duration_just_over_the_limit_as_over_it(self) -> None:
        error = check_duration(8.701, subject="The video in 'Video'", maximum=8.7)
        assert str(error).startswith("The video in 'Video' is 8.701 seconds long and the limit is 8.7.")

    def test_reports_a_duration_under_a_non_round_minimum_as_under_it(self) -> None:
        error = check_duration(1.9455, subject="The video in 'Video'", minimum=1.946)
        assert str(error).startswith("The video in 'Video' is 1.9455 seconds long and the minimum is 1.946.")

    def test_reports_a_duration_under_a_long_minimum_as_under_it(self) -> None:
        error = check_duration(2.046665, subject="The video in 'Video'", minimum=2.0466666666)
        assert str(error).startswith("The video in 'Video' is 2.04666 seconds long and the minimum is 2.04667.")

    def test_rounds_a_long_minimum_up_so_a_clip_under_it_reads_as_under_it(self) -> None:
        # Six significant figures would show 1234.56, under the minimum; it is shown rounded up.
        error = check_duration(1234.5, subject="The video in 'Video'", minimum=1234.5649)
        assert str(error).startswith("The video in 'Video' is 1234.50 seconds long and the minimum is 1234.57.")

    def test_prints_a_duration_in_full_when_no_rounding_reads_as_past_the_limit(self) -> None:
        error = check_duration(1.000000000001, subject="The video in 'Video'", maximum=1.0000000000001)
        assert str(error).startswith("The video in 'Video' is 1.000000000001 seconds long and the limit is 1.")

    def test_uses_the_given_fix(self) -> None:
        error = check_duration(20, subject="The video in 'Video'", maximum=10, fix="Pick a shorter clip.")
        assert str(error).endswith("Pick a shorter clip.")
