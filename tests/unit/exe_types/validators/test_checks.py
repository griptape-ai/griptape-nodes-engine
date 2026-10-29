"""Tests for the checks behind the media and list validators, each comparing one reading with a limit."""

from __future__ import annotations

import pytest

from griptape_nodes.exe_types.node_error import NodeError
from griptape_nodes.exe_types.validators._checks import check_count, input_subject
from griptape_nodes.exe_types.validators._media_checks import (
    check_aspect_ratio_exact,
    check_aspect_ratio_range,
    check_audio_codec,
    check_dimensions,
    check_duration,
    check_frame_count,
    check_frame_rate,
    check_mime_type,
    check_size,
    check_video_codec,
)
from griptape_nodes.utils.media_probe import ImageInfo

SUBJECT = "Image 2 in 'reference images'"
DOCS = "https://example.com/limits"


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

    def test_refuses_a_named_container_the_provider_doesnt_list(self) -> None:
        error = check_mime_type("application/mxf", subject=SUBJECT, allowed=("video/mp4", "video/quicktime"))
        assert str(error) == f"{SUBJECT} is in MXF format. Convert it to MP4 or MOV."

    def test_names_an_unmapped_container_in_a_refusal(self) -> None:
        error = check_mime_type("video/x-ffprobe-something_new", subject=SUBJECT, allowed=("video/mp4",))
        assert str(error) == f"{SUBJECT} is in SOMETHING_NEW format. Convert it to MP4."

    def test_refuses_a_still_image_by_its_image_format(self) -> None:
        error = check_mime_type("image/png", subject=SUBJECT, allowed=("video/mp4",))
        assert str(error) == f"{SUBJECT} is in PNG format. Convert it to MP4."

    def test_refuses_an_mpeg_program_stream_where_mp3_is_asked(self) -> None:
        error = check_mime_type("video/mpeg", subject=SUBJECT, allowed=("audio/mpeg",))
        assert str(error) == f"{SUBJECT} is in MPEG format. Convert it to MP3."

    def test_accepts_an_ogg_opus_file_listed_as_audio_opus(self) -> None:
        assert check_mime_type("audio/ogg", subject=SUBJECT, allowed=("audio/opus", "audio/mpeg")) is None


class TestCheckVideoCodec:
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

    @pytest.mark.parametrize("allowed", [("H.264", "H.265"), ("h264",), ("AVC", "H.264")])
    def test_accepts_a_codec_written_the_way_provider_docs_do(self, allowed: tuple[str, ...]) -> None:
        assert check_video_codec("h264", subject=SUBJECT, allowed=allowed) is None

    def test_lists_a_codec_once_when_the_provider_names_it_twice(self) -> None:
        error = check_video_codec("vp9", subject=SUBJECT, allowed=("hevc", "H.265"))
        assert str(error) == f"{SUBJECT} is encoded as VP9. Re-encode it as H.265."


class TestCheckAudioCodec:
    def test_refuses_codecs_given_as_one_string(self) -> None:
        with pytest.raises(TypeError, match="pass a tuple"):
            check_audio_codec("mp3", subject=SUBJECT, allowed="mp3")

    def test_names_pcm_byte_order(self) -> None:
        error = check_audio_codec("pcm_s16be", subject=SUBJECT, allowed=("mp3", "pcm_s16le"))
        assert str(error) == (
            f"{SUBJECT} is encoded as 16-bit PCM (big-endian). Re-encode it as MP3 or 16-bit PCM (little-endian)."
        )

    @pytest.mark.parametrize(("codec", "allowed"), [("opus", ("Opus",)), ("aac", ("AAC",)), ("vorbis", ("Vorbis",))])
    def test_matches_an_audio_codec_however_a_provider_writes_it(self, codec: str, allowed: tuple[str, ...]) -> None:
        assert check_audio_codec(codec, subject=SUBJECT, allowed=allowed) is None


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


class TestInputSubject:
    def test_names_a_position_in_a_list(self) -> None:
        assert input_subject("reference images", noun="image", position=2) == SUBJECT

    def test_names_a_single_input(self) -> None:
        assert input_subject("start frame", noun="image") == "The image in 'start frame'"

    def test_keeps_a_nouns_own_capitals_at_a_position(self) -> None:
        assert input_subject("icons", noun="SVG icon", position=2) == "SVG icon 2 in 'icons'"
        assert input_subject("loras", noun="LoRA file", position=1) == "LoRA file 1 in 'loras'"
