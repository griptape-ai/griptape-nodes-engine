"""Tests for the media validators, on real files and on readings the readers can return."""

from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING, Any

import pytest
from griptape.artifacts import ImageArtifact
from PIL import Image

from griptape_nodes.exe_types.validators.media import (
    AllowedFormats,
    AspectRatioRange,
    AudioCodecs,
    Duration,
    ExactAspectRatios,
    FrameCount,
    FrameRate,
    MaxFileSize,
    PixelArea,
    SideRange,
    VideoCodecs,
)
from griptape_nodes.utils import media_probe
from griptape_nodes.utils.media_probe import (
    AudioProbe,
    ImageInfo,
    MediaInfo,
    MediaNeed,
    VideoProbe,
    local_size_bytes,
    read_media,
)
from tests.unit.exe_types.validators.media_files import (
    HAS_FFMPEG,
    HEIC_PHOTO,
    audio_file,
    data_uri,
    image_file,
    png_bytes,
    video_file,
)

if TYPE_CHECKING:
    from pathlib import Path

    from griptape_nodes.exe_types.validators.base import MediaValidator

SUBJECT = "Image 2 in 'reference images'"
DOCS = "https://example.com/limits"

needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg and ffprobe are needed to make and read a clip")


def _messages(validators: list[MediaValidator], media: MediaInfo, value: Any = None) -> list[str]:
    """What each validator reports for one input, in order, leaving out those it passes."""
    errors = [validator.check_media(value, media, subject=SUBJECT, docs_url=None) for validator in validators]
    return [str(error) for error in errors if error is not None]


def _image(value: Any) -> MediaInfo:
    return read_media(value, {MediaNeed.SIZE, MediaNeed.IMAGE}, kind="image")


def _video(value: Any) -> MediaInfo:
    return read_media(value, {MediaNeed.SIZE, MediaNeed.VIDEO}, kind="video")


def _audio(value: Any) -> MediaInfo:
    return read_media(value, {MediaNeed.SIZE, MediaNeed.AUDIO}, kind="audio")


class TestOnRealImages:
    KLING = (
        AllowedFormats("image/jpeg", "image/png"),
        MaxFileSize(10 * 1024 * 1024),
        SideRange(min=300),
        AspectRatioRange("1:2.5", "2.5:1"),
    )

    def test_accepts_an_image_within_every_limit(self, tmp_path: Path) -> None:
        assert _messages(list(self.KLING), _image(str(image_file(tmp_path, 800, 600)))) == []

    def test_reports_every_limit_an_image_breaks(self, tmp_path: Path) -> None:
        media = _image(str(image_file(tmp_path, 200, 1000, "GIF")))
        assert _messages(list(self.KLING), media) == [
            f"{SUBJECT} is in GIF format. Convert it to JPEG or PNG.",
            f"{SUBJECT} is 200x1000 and each side must be at least 300 pixels. Use a larger one.",
            f"{SUBJECT} is 200x1000, which is too tall. The aspect ratio must be between 1:2.5 and 2.5:1. Crop it.",
        ]

    def test_checks_the_largest_and_smallest_side(self, tmp_path: Path) -> None:
        media = _image(str(image_file(tmp_path, 100, 1000)))
        assert _messages([SideRange(max=800)], media) == [
            f"{SUBJECT} is 100x1000 and each side must be at most 800 pixels. Resize it smaller."
        ]

    def test_checks_the_area(self, tmp_path: Path) -> None:
        media = _image(str(image_file(tmp_path, 400, 300)))
        assert _messages([PixelArea(max_megapixels=0.1)], media) == [
            f"{SUBJECT} is 400x300 (0.12 megapixels) and the limit is 0.1 megapixels. Resize it smaller."
        ]
        assert _messages([PixelArea(min_megapixels=0.2)], media) == [
            f"{SUBJECT} is 400x300 (0.1 megapixels) and the minimum is 0.2 megapixels. Use a larger one."
        ]

    def test_checks_exact_ratios(self, tmp_path: Path) -> None:
        media = _image(str(image_file(tmp_path, 400, 300)))
        assert _messages([ExactAspectRatios("16:9", "9:16")], media) == [
            f"{SUBJECT} is 400x300, and its aspect ratio must be 16:9 or 9:16. Crop it."
        ]
        assert _messages([ExactAspectRatios("4:3")], media) == []

    def test_accepts_decimal_ratios(self, tmp_path: Path) -> None:
        media = _image(str(image_file(tmp_path, 400, 100)))
        assert _messages([AspectRatioRange("1:2.5", "2.5:1")], media) == [
            f"{SUBJECT} is 400x100, which is too wide. The aspect ratio must be between 1:2.5 and 2.5:1. Crop it."
        ]

    def test_judges_an_exif_rotated_photo_by_its_displayed_shape(self) -> None:
        exif = Image.Exif()
        exif[0x0112] = 6
        photo = Image.new("RGB", (4032, 3024))
        buffer = BytesIO()
        photo.save(buffer, format="JPEG", exif=exif.tobytes())
        media = _image(data_uri(buffer.getvalue(), "image/jpeg"))
        assert _messages([AspectRatioRange("9:16", "1:1")], media) == []

    def test_checks_the_file_size_and_a_strict_limit(self, tmp_path: Path) -> None:
        path = image_file(tmp_path, 40, 30)
        size = path.stat().st_size
        media = _image(str(path))
        assert media.size_bytes == size
        assert _messages([MaxFileSize(size)], media) == []
        assert len(_messages([MaxFileSize(size, strict=True)], media)) == 1
        assert len(_messages([MaxFileSize(10)], media)) == 1

    def test_uses_a_given_size_fix(self, tmp_path: Path) -> None:
        media = _image(str(image_file(tmp_path, 400, 300)))
        (message,) = _messages([MaxFileSize(10, fix="Turn on Auto Image Resize.")], media)
        assert message.endswith("Turn on Auto Image Resize.")

    @pytest.mark.parametrize("image_format", ["DDS", "QOI"])
    def test_refuses_a_format_pillow_reads_but_has_no_mime_type_for(self, image_format: str) -> None:
        media = _image(data_uri(png_bytes(64, 64, image_format)))
        assert _messages([AllowedFormats("image/jpeg", "image/png")], media) == [
            f"{SUBJECT} is in {image_format} format. Convert it to JPEG or PNG."
        ]

    def test_links_the_docs_page(self, tmp_path: Path) -> None:
        media = _image(str(image_file(tmp_path, 100, 100)))
        error = SideRange(min=300).check_media(None, media, subject=SUBJECT, docs_url=DOCS)
        assert error is not None
        assert error.links
        assert error.links[0].url == DOCS

    def test_skips_a_remote_image(self) -> None:
        media = _image("https://example.com/image.gif")
        assert media == MediaInfo(location="https://example.com/image.gif")
        assert _messages(list(self.KLING), media) == []

    def test_reduces_an_in_memory_image_to_a_string_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[Any] = []
        coerce = media_probe.coerce_media_url_or_data_uri

        def counting(value: Any, *, kind: Any) -> str | None:
            calls.append(value)
            return coerce(value, kind=kind)

        monkeypatch.setattr(media_probe, "coerce_media_url_or_data_uri", counting)
        artifact = ImageArtifact(png_bytes(400, 300), format="png", width=400, height=300)
        media = _image(artifact)
        assert media.image == ImageInfo(400, 300, "image/png")
        assert sum(1 for value in calls if value is artifact) == 1


class TestOnARealHeicPhoto:
    """A HEIC photo, which Pillow can't open without a plugin the engine doesn't install."""

    LIMITS = (
        SideRange(min=300, max=6000),
        PixelArea(min_megapixels=1),
        AspectRatioRange("1:3", "3:1"),
        ExactAspectRatios("16:9"),
    )

    @pytest.mark.parametrize("listed", [("image/heic",), ("image/heif",), ("image/heic", "image/heif")])
    def test_accepts_it_where_either_heif_name_is_listed(self, listed: tuple[str, ...]) -> None:
        for value in (str(HEIC_PHOTO), data_uri(HEIC_PHOTO.read_bytes(), "image/heic")):
            assert _messages([AllowedFormats("image/jpeg", *listed)], _image(value)) == []

    def test_refuses_it_by_its_real_format_where_heif_is_not_listed(self, tmp_path: Path) -> None:
        # Named .jpg, it is still a HEIC.
        renamed = tmp_path / "photo.jpg"
        renamed.write_bytes(HEIC_PHOTO.read_bytes())
        assert _messages([AllowedFormats("image/jpeg", "image/png")], _image(str(renamed))) == [
            f"{SUBJECT} is in HEIC format. Convert it to JPEG or PNG."
        ]

    def test_skips_the_size_and_shape_checks_it_cannot_read(self) -> None:
        media = _image(str(HEIC_PHOTO))
        assert media.image is None
        assert media.image_format == "image/heif"
        assert _messages(list(self.LIMITS), media) == []

    def test_still_checks_its_file_size(self) -> None:
        media = _image(str(HEIC_PHOTO))
        assert media.size_bytes == HEIC_PHOTO.stat().st_size
        assert len(_messages([MaxFileSize(100)], media)) == 1


@needs_ffmpeg
class TestOnRealVideos:
    def test_reads_and_checks_a_real_clip(self, tmp_path: Path) -> None:
        media = _video(str(video_file(tmp_path, width=320, height=240, fps=24, seconds=2)))
        assert media.video is not None
        assert media.video.mime_type == "video/mp4"
        validators: list[MediaValidator] = [
            AllowedFormats("video/mp4"),
            VideoCodecs("h264"),
            SideRange(min=200, max=1920),
            AspectRatioRange("1:2", "2:1"),
            Duration(min=1, max=10),
            FrameRate(min=24, max=60),
            FrameCount(min=24, max=100),
            MaxFileSize(10 * 1024 * 1024),
        ]
        assert _messages(validators, media) == []

    def test_reports_every_limit_a_real_clip_breaks(self, tmp_path: Path) -> None:
        media = _video(str(video_file(tmp_path, width=320, height=240, fps=24, seconds=2)))
        validators: list[MediaValidator] = [
            AllowedFormats("video/quicktime"),
            VideoCodecs("hevc"),
            SideRange(min=480),
            ExactAspectRatios("16:9"),
            Duration(min=3),
            FrameRate(min=30),
            FrameCount(max=24),
        ]
        assert _messages(validators, media) == [
            f"{SUBJECT} is in MP4 format. Convert it to MOV.",
            f"{SUBJECT} is encoded as H.264. Re-encode it as H.265.",
            f"{SUBJECT} is 320x240 and each side must be at least 480 pixels. Use a larger one.",
            f"{SUBJECT} is 320x240, and its aspect ratio must be 16:9. Crop it.",
            f"{SUBJECT} is 2.00 seconds long and the minimum is 3. Use a longer clip.",
            f"{SUBJECT} is 24 fps and the minimum is 30 fps. Re-export it at a higher frame rate.",
            f"{SUBJECT} has 48 frames and the limit is 24. Shorten it, or lower its frame rate.",
        ]

    def test_reads_a_real_clips_size_without_probing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = video_file(tmp_path)
        monkeypatch.setattr(media_probe, "local_video_probe", lambda _v: pytest.fail("a size limit needs no probe"))
        media = read_media(str(path), {MediaNeed.SIZE}, kind="video")
        assert media.size_bytes == path.stat().st_size
        assert len(_messages([MaxFileSize(1)], media)) == 1


@needs_ffmpeg
class TestOnRealAudio:
    def test_reads_and_checks_a_real_mp3(self, tmp_path: Path) -> None:
        media = _audio(str(audio_file(tmp_path, seconds=3)))
        assert media.audio is not None
        validators: list[MediaValidator] = [AllowedFormats("audio/mpeg"), AudioCodecs("mp3"), Duration(min=2, max=5)]
        assert _messages(validators, media) == []

    def test_reports_every_limit_a_real_mp3_breaks(self, tmp_path: Path) -> None:
        media = _audio(str(audio_file(tmp_path, seconds=3)))
        validators: list[MediaValidator] = [
            AllowedFormats("audio/wav"),
            AudioCodecs("pcm_s16le"),
            Duration(max=1),
        ]
        assert _messages(validators, media) == [
            f"{SUBJECT} is in MP3 format. Convert it to WAV.",
            f"{SUBJECT} is encoded as MP3. Re-encode it as 16-bit PCM (little-endian).",
            f"{SUBJECT} is 3.00 seconds long and the limit is 1. Shorten it.",
        ]


class TestVideoReadings:
    """Readings that are hard to make with a real clip: variable frame rates, anamorphic frames."""

    @pytest.mark.parametrize(
        ("average", "nominal", "validator", "expected"),
        [
            # A 60 fps screen recording averaging 19 fps: the minimum uses the higher, declared rate.
            (19.1, 60.0, FrameRate(min=24, max=60), None),
            # A clip declaring 120 fps but averaging 50: the maximum uses the lower, average rate.
            (50.0, 120.0, FrameRate(min=24, max=60), None),
            # Both rates under the minimum still fail, quoting the higher.
            (15.0, 20.0, FrameRate(min=24), "is 20 fps and the minimum is 24 fps."),
        ],
        ids=["vfr-under-min-average", "vfr-over-max-nominal", "both-under-min"],
    )
    def test_checks_each_frame_rate_bound_against_the_easier_rate(
        self, average: float, nominal: float, validator: FrameRate, expected: str | None
    ) -> None:
        media = MediaInfo(video=VideoProbe(info=None, seconds=5.0, fps=average, nominal_fps=nominal))
        messages = _messages([validator], media)
        if expected is None:
            assert messages == []
        else:
            assert len(messages) == 1
            assert expected in messages[0]

    @pytest.mark.parametrize(
        ("stream", "container", "validator", "expected"),
        [
            # A 10 s video with a 31 s audio track: the maximum is checked against the video.
            (10.0, 31.0, Duration(max=30), None),
            # A 2.9 s video with 3.5 s of audio: the minimum is checked against the longer reading.
            (2.9, 3.5, Duration(min=3), None),
            (31.0, 32.0, Duration(max=30), "is 31.00 seconds long and the limit is 30."),
            (2.0, 2.5, Duration(min=3), "is 2.50 seconds long and the minimum is 3."),
        ],
        ids=["long-audio-under-max", "long-audio-over-min", "both-over-max", "both-under-min"],
    )
    def test_checks_each_duration_bound_against_the_easier_reading(
        self, stream: float, container: float, validator: Duration, expected: str | None
    ) -> None:
        media = MediaInfo(video=VideoProbe(info=None, seconds=stream, container_seconds=container))
        messages = _messages([validator], media)
        if expected is None:
            assert messages == []
        else:
            assert len(messages) == 1
            assert expected in messages[0]

    def test_uses_a_given_duration_fix(self) -> None:
        media = MediaInfo(video=VideoProbe(info=None, seconds=1.0))
        assert _messages([Duration(min=3, fix="Pick a longer clip.")], media) == [
            f"{SUBJECT} is 1.00 seconds long and the minimum is 3. Pick a longer clip."
        ]

    def test_a_library_can_name_its_own_trim_node(self) -> None:
        # The engine's own suggestions name no node; a library that ships one says so with `fix=`.
        fix = "Use a Trim Video node to shorten it."
        media = MediaInfo(video=VideoProbe(info=None, seconds=12.0, fps=24.0, frames=288))
        assert _messages([Duration(max=10, fix=fix), FrameCount(max=240, fix=fix)], media) == [
            f"{SUBJECT} is 12.00 seconds long and the limit is 10. {fix}",
            f"{SUBJECT} has 288 frames and the limit is 240. {fix}",
        ]

    def test_checks_frames_against_the_reading_easier_to_meet(self) -> None:
        # No recorded count: 3 s at 24 fps average (72 frames) or 30 fps declared (90 frames).
        media = MediaInfo(video=VideoProbe(info=None, seconds=3.0, fps=24.0, nominal_fps=30.0))
        assert _messages([FrameCount(min=80, max=80)], media) == []
        assert _messages([FrameCount(max=60)], media) == [
            f"{SUBJECT} has 72 frames and the limit is 60. Shorten it, or lower its frame rate."
        ]
        assert _messages([FrameCount(min=100)], media) == [
            f"{SUBJECT} has 90 frames and the minimum is 100. Use a longer clip."
        ]

    def test_passes_a_shape_check_when_the_displayed_frame_fits(self) -> None:
        # 1440x1080 HDV with 4:3 pixels plays at 16:9.
        probe = VideoProbe(info=ImageInfo(1440, 1080, None), seconds=5.0, display_info=ImageInfo(1920, 1080, None))
        media = MediaInfo(video=probe)
        assert _messages([ExactAspectRatios("16:9"), AspectRatioRange("16:9", "16:9")], media) == []

    def test_checks_pixel_limits_against_the_stored_frame(self) -> None:
        probe = VideoProbe(info=ImageInfo(1440, 1080, None), seconds=5.0, display_info=ImageInfo(1920, 1080, None))
        assert _messages([SideRange(max=1500)], MediaInfo(video=probe)) == []

    def test_refuses_a_shape_neither_the_stored_nor_displayed_frame_fits(self) -> None:
        # 1440x1080 at 4:3 pixels plays at 16:9; neither shape is square.
        probe = VideoProbe(info=ImageInfo(1440, 1080, None), seconds=5.0, display_info=ImageInfo(1920, 1080, None))
        assert _messages([ExactAspectRatios("1:1")], MediaInfo(video=probe)) == [
            f"{SUBJECT} is 1440x1080, and its aspect ratio must be 1:1. Crop it."
        ]

    @pytest.mark.parametrize(
        ("validator", "expected"),
        [
            (FrameRate(max=30), f"{SUBJECT} is 60 fps and the maximum is 30 fps. Re-export it at a lower frame rate."),
            (
                FrameCount(max=100),
                (f"{SUBJECT} has 300 frames and the limit is 100. Shorten it, or lower its frame rate."),
            ),
        ],
    )
    def test_reports_a_maximum_without_checking_an_unset_minimum(
        self, validator: MediaValidator, expected: str
    ) -> None:
        media = MediaInfo(video=VideoProbe(info=None, seconds=5.0, fps=60.0, frames=300))
        assert _messages([validator], media) == [expected]

    def test_passes_a_clip_within_a_maximum_with_no_minimum(self) -> None:
        media = MediaInfo(video=VideoProbe(info=None, seconds=5.0, fps=24.0, frames=120))
        assert _messages([FrameRate(max=30), FrameCount(max=150)], media) == []

    def test_skips_every_check_for_a_video_it_could_not_read(self) -> None:
        validators: list[MediaValidator] = [
            AllowedFormats("video/mp4"),
            VideoCodecs("h264"),
            SideRange(min=1),
            AspectRatioRange("1:1", "1:1"),
            ExactAspectRatios("1:1"),
            Duration(min=100),
            FrameRate(min=100),
            FrameCount(min=100),
            MaxFileSize(1),
        ]
        assert _messages(validators, MediaInfo()) == []

    def test_skips_a_remote_video_without_probing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            media_probe.subprocess, "run", lambda *_a, **_k: pytest.fail("a remote video must not be probed")
        )
        assert _video("https://example.com/clip.mp4") == MediaInfo(location="https://example.com/clip.mp4")


class TestAspectRatioRangeBounds:
    def test_refuses_bounds_given_widest_first_when_built(self) -> None:
        with pytest.raises(ValueError, match="pass the tall bound first"):
            AspectRatioRange("16:9", "9:16")


class TestAudioReadings:
    def test_accepts_an_mp4_videos_audio_where_video_mp4_is_listed(self) -> None:
        media = MediaInfo(audio=AudioProbe(mime_type="audio/mp4", codec="aac", profile="LC", seconds=3.0))
        assert _messages([AllowedFormats("audio/mpeg", "video/mp4")], media) == []

    def test_lists_only_the_names_the_provider_wrote_when_refusing(self) -> None:
        media = MediaInfo(audio=AudioProbe(mime_type="audio/flac", codec="flac", profile=None, seconds=3.0))
        assert _messages([AllowedFormats("audio/mpeg", "video/mp4")], media) == [
            f"{SUBJECT} is in FLAC format. Convert it to MP3 or MP4."
        ]

    @pytest.mark.parametrize(
        ("reading", "listed"),
        [("audio/x-matroska", "video/x-matroska"), ("audio/3gpp", "video/3gpp"), ("audio/webm", "video/webm")],
    )
    def test_accepts_a_videos_audio_where_its_container_is_listed(self, reading: str, listed: str) -> None:
        media = MediaInfo(audio=AudioProbe(mime_type=reading, codec="aac", profile="LC", seconds=3.0))
        assert _messages([AllowedFormats(listed)], media) == []

    def test_suggests_an_audio_editor_for_audio_that_is_too_long(self) -> None:
        media = MediaInfo(audio=AudioProbe(mime_type="audio/wav", codec="pcm_s16le", profile=None, seconds=3.0))
        assert _messages([Duration(max=1), Duration(min=5)], media) == [
            f"{SUBJECT} is 3.00 seconds long and the limit is 1. Shorten it.",
            f"{SUBJECT} is 3.00 seconds long and the minimum is 5. Use a longer clip.",
        ]

    def test_uses_a_given_fix_for_audio_that_is_too_long(self) -> None:
        media = MediaInfo(audio=AudioProbe(mime_type="audio/wav", codec="pcm_s16le", profile=None, seconds=3.0))
        assert _messages([Duration(max=1, fix="Pick a shorter take.")], media) == [
            f"{SUBJECT} is 3.00 seconds long and the limit is 1. Pick a shorter take."
        ]

    def test_codec_validators_skip_the_other_kind(self) -> None:
        audio = MediaInfo(audio=AudioProbe(mime_type="audio/mpeg", codec="mp3", profile=None, seconds=3.0))
        video = MediaInfo(video=VideoProbe(info=None, seconds=3.0, codec="h264"))
        assert _messages([VideoCodecs("hevc")], audio) == []
        assert _messages([AudioCodecs("aac")], video) == []

    def test_reports_the_audio_container_as_the_inputs_format(self) -> None:
        media = MediaInfo(audio=AudioProbe(mime_type="audio/flac", codec="flac", profile=None, seconds=3.0))
        assert media.mime_type == "audio/flac"


class TestArguments:
    """A limit that can't be checked fails when the validator is built, not when a node runs."""

    @pytest.mark.parametrize("bound", ["16x9", "2.5", "3:0", "0:1", "a:b", ""])
    def test_refuses_a_bound_that_is_not_a_ratio(self, bound: str) -> None:
        with pytest.raises(ValueError, match="is not a ratio such as '16:9'"):
            AspectRatioRange("1:2", bound)

    @pytest.mark.parametrize("ratios", [(), ("16x9",), ("1:inf",)])
    def test_refuses_no_exact_ratio_or_a_bad_one(self, ratios: tuple[str, ...]) -> None:
        with pytest.raises(ValueError, match="Attempted to check"):
            ExactAspectRatios(*ratios)

    def test_refuses_names_passed_as_one_tuple(self) -> None:
        with pytest.raises(TypeError, match="pass each one as its own argument"):
            AllowedFormats(("image/png", "image/jpeg"))  # pyright: ignore[reportArgumentType]
        with pytest.raises(TypeError, match="pass each one as its own argument"):
            VideoCodecs(("h264",))  # pyright: ignore[reportArgumentType]

    def test_refuses_a_codec_it_does_not_know(self) -> None:
        with pytest.raises(ValueError, match="isn't a codec this module knows"):
            VideoCodecs("H.264/AVC")
        with pytest.raises(ValueError, match="isn't a codec this module knows"):
            AudioCodecs("MPEG Layer 3")

    def test_accepts_codecs_however_a_provider_writes_them(self) -> None:
        assert VideoCodecs("H.264", "HEVC").codecs == ("H.264", "HEVC")

    def test_validators_are_frozen_and_hashable(self) -> None:
        validator = SideRange(min=300)
        with pytest.raises(AttributeError):
            validator.min = 1  # pyright: ignore[reportAttributeAccessIssue]
        assert hash(validator) == hash(SideRange(min=300))
        assert hash(AllowedFormats("image/png")) == hash(AllowedFormats("image/png"))


def test_local_size_bytes_still_reads_outside_a_pass(tmp_path: Path) -> None:
    """A reader called outside `reading_once` reads the file, as before."""
    path = image_file(tmp_path, 10, 10)
    assert local_size_bytes(str(path), kind="image") == path.stat().st_size
