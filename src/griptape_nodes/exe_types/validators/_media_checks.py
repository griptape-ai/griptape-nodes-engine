"""The checks behind the validators in `media`: each compares one reading of a file with a limit and writes the message.

A check skips a reading that is None, which is what the readers in
`griptape_nodes.utils.media_probe` return for anything they couldn't read locally.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from griptape_nodes.exe_types.validators._checks import limit_error

if TYPE_CHECKING:
    from collections.abc import Sequence

    from griptape_nodes.exe_types.node_error import NodeError
    from griptape_nodes.utils.media_probe import ImageInfo


_BYTES_PER_MB = 1024 * 1024


# How far past a frame-rate limit still passes, as a fraction of the limit. Providers write limits
# like "24-60fps"; NTSC footage at 23.976 or 59.94 fps is within 0.1% of those.
FRAME_RATE_TOLERANCE = 0.01


def check_size(  # noqa: PLR0913
    size_bytes: int | None,
    *,
    maximum_bytes: int,
    subject: str,
    fix: str | None = None,
    strict: bool = False,
    docs_url: str | None = None,
) -> NodeError | None:
    """An error when an input file is larger than the provider accepts.

    Args:
        size_bytes: The file's size, from `local_size_bytes`. None skips the check.
        maximum_bytes: The largest file the provider accepts, or with `strict`, the size every file
            must be under.
        subject: The input, from `input_subject`.
        fix: What the artist can do about it. Defaults to "Use a smaller file."
        strict: For a provider that writes "less than 30 MB": a file of exactly `maximum_bytes` is
            refused too.
        docs_url: The provider's page documenting the limit.
    """
    if size_bytes is None:
        return None
    fix = fix or "Use a smaller file."
    if strict and size_bytes >= maximum_bytes:
        limit = _format_limit(maximum_bytes / _BYTES_PER_MB)
        # A file at exactly the limit is the one a strict limit adds; printed past it, it would round
        # onto the allowed side and read as smaller than the limit.
        if size_bytes == maximum_bytes:
            shown = f"exactly {limit}"
        else:
            shown = _format_past(size_bytes / _BYTES_PER_MB, maximum_bytes / _BYTES_PER_MB, 1)
        return limit_error(f"{subject} is {shown} MB and it must be under {limit} MB. {fix}", docs_url)
    if size_bytes <= maximum_bytes:
        return None
    return limit_error(
        f"{subject} is {_format_past(size_bytes / _BYTES_PER_MB, maximum_bytes / _BYTES_PER_MB, 1)} MB and the limit "
        f"is {_format_limit(maximum_bytes / _BYTES_PER_MB)} MB. {fix}",
        docs_url,
    )


def check_dimensions(  # noqa: PLR0913
    info: ImageInfo | None,
    *,
    subject: str,
    min_side: int | None = None,
    max_side: int | None = None,
    min_megapixels: float | None = None,
    max_megapixels: float | None = None,
    docs_url: str | None = None,
) -> NodeError | None:
    """An error when an image or video frame's pixel size is outside what the provider accepts.

    Args:
        info: The pixel size, from `local_image_info` or a `local_video_probe` result's `info`. None skips
            the check.
        subject: The input, from `input_subject`.
        min_side: The fewest pixels accepted for the width and for the height.
        max_side: The most pixels accepted for the width and for the height.
        min_megapixels: The fewest pixels accepted in total, in millions.
        max_megapixels: The most pixels accepted in total, in millions.
        docs_url: The provider's page documenting the limit.
    """
    if info is None:
        return None
    size = f"{info.width}x{info.height}"
    if min_side is not None and min(info.width, info.height) < min_side:
        return limit_error(
            f"{subject} is {size} and each side must be at least {min_side} pixels. Use a larger one.", docs_url
        )
    if max_side is not None and max(info.width, info.height) > max_side:
        return limit_error(
            f"{subject} is {size} and each side must be at most {max_side} pixels. Resize it smaller.", docs_url
        )
    megapixels = info.width * info.height / 1_000_000
    if min_megapixels is not None and megapixels < min_megapixels:
        return limit_error(
            f"{subject} is {size} ({_format_past(megapixels, min_megapixels, 1)} megapixels) and the minimum is "
            f"{_format_limit(min_megapixels, round_up=True)} megapixels. Use a larger one.",
            docs_url,
        )
    if max_megapixels is not None and megapixels > max_megapixels:
        return limit_error(
            f"{subject} is {size} ({_format_past(megapixels, max_megapixels, 1)} megapixels) and the limit is "
            f"{_format_limit(max_megapixels)} megapixels. Resize it smaller.",
            docs_url,
        )
    return None


def check_aspect_ratio_range(
    info: ImageInfo | None, *, subject: str, widest: str, tallest: str, docs_url: str | None = None
) -> NodeError | None:
    """An error when an image or video frame is wider or taller than the provider accepts.

    Args:
        info: The pixel size, from `local_image_info` or a `local_video_probe` result's `info`. None skips
            the check.
        subject: The input, from `input_subject`.
        widest: The widest ratio accepted, as the provider writes it, such as "3:1".
        tallest: The tallest ratio accepted, such as "1:3".
        docs_url: The provider's page documenting the limit.

    Raises:
        ValueError: If `widest` or `tallest` isn't a ratio such as "16:9", or `tallest` is wider than
            `widest`, which no frame could pass.
    """
    widest_ratio = _parse_ratio(widest)
    tallest_ratio = _parse_ratio(tallest)
    if tallest_ratio > widest_ratio:
        msg = (
            f"Attempted to check an aspect ratio range. Failed because the tallest ratio, {tallest}, is "
            f"wider than the widest, {widest}; pass the tall bound first, such as ('9:16', '16:9')."
        )
        raise ValueError(msg)
    if info is None or info.height == 0:
        return None
    ratio = info.width / info.height
    if tallest_ratio <= ratio <= widest_ratio:
        return None
    if ratio > widest_ratio:
        shape = "wide"
    else:
        shape = "tall"
    return limit_error(
        f"{subject} is {info.width}x{info.height}, which is too {shape}. The aspect ratio must be between "
        f"{tallest} and {widest}. Crop it.",
        docs_url,
    )


def check_aspect_ratio_exact(
    info: ImageInfo | None, *, subject: str, ratios: Sequence[str], docs_url: str | None = None
) -> NodeError | None:
    """An error when an image or video frame isn't one of the exact aspect ratios the provider accepts.

    For a provider that takes only certain shapes, such as "16:9 or 9:16", rather than a range. A
    frame matches a ratio when either side is within a pixel of what the ratio makes it from the other,
    so a size rounded to whole pixels, such as 854x480 for 16:9 or 406x720 for 9:16, still matches.

    Args:
        info: The pixel size, from `local_image_info` or a `local_video_probe` result's `info`. None skips
            the check.
        subject: The input, from `input_subject`.
        ratios: The ratios accepted, as the provider writes them, such as ("16:9", "9:16").
        docs_url: The provider's page documenting the limit.

    Raises:
        ValueError: If `ratios` is empty or one of them isn't a ratio such as "16:9".
        TypeError: If `ratios` is a single string rather than a sequence of ratios.
    """
    _require_valid_exact_ratios(ratios)
    if info is None or info.height == 0:
        return None
    for name in ratios:
        ratio = _parse_ratio(name)
        # Allow a pixel of rounding on whichever side was derived from the other.
        if abs(info.width / ratio - info.height) <= 1 or abs(info.height * ratio - info.width) <= 1:
            return None
    return limit_error(
        f"{subject} is {info.width}x{info.height}, and its aspect ratio must be {_join_or(list(ratios))}. Crop it.",
        docs_url,
    )


def check_mime_type(
    mime_type: str | None,
    *,
    subject: str,
    allowed: Sequence[str],
    docs_url: str | None = None,
    audio: bool = False,
) -> NodeError | None:
    """An error when an input's format isn't one the provider accepts.

    Args:
        mime_type: The input's MIME type, such as from `local_image_info`. None skips the check.
        subject: The input, from `input_subject`.
        allowed: The MIME types the provider accepts, such as ("image/jpeg", "image/png").
        docs_url: The provider's page documenting the limit.
        audio: True when `mime_type` was read from an audio input, such as `local_audio_probe`'s.
            A video container in `allowed` then also accepts the audio read from it, such as an
            MP4's, which reads as audio/mp4.

    Raises:
        TypeError: If `allowed` is a single string rather than a sequence of MIME types.
    """
    _require_mime_type_sequence(allowed)
    also_matching = _audio_readings_of(allowed) if audio else ()
    return _mime_type_error(mime_type, subject=subject, allowed=allowed, docs_url=docs_url, also_matching=also_matching)


def _mime_type_error(
    mime_type: str | None,
    *,
    subject: str,
    allowed: Sequence[str],
    docs_url: str | None,
    also_matching: Sequence[str] = (),
) -> NodeError | None:
    """`check_mime_type`, with `also_matching` names that widen the match but aren't listed in the message."""
    if mime_type is None or not allowed or _is_allowed(mime_type, (*allowed, *also_matching)):
        return None
    # Names that differ but read the same, such as audio/wav and audio/x-wav, are listed once.
    formats = _join_or(list(dict.fromkeys(_format_name(name) for name in allowed)))
    # Pillow names every HEIC and HEIF photo image/heif; artists know a phone photo as HEIC.
    current = _INPUT_FORMAT_NAMES.get(_canonical_mime_type(mime_type)) or _format_name(mime_type)
    return limit_error(f"{subject} is in {current} format. Convert it to {formats}.", docs_url)


def check_video_codec(
    codec: str | None, *, subject: str, allowed: Sequence[str], docs_url: str | None = None
) -> NodeError | None:
    """An error when a video's codec isn't one the provider accepts.

    Args:
        codec: The video stream's codec as ffprobe names it, from `local_video_probe`, such as "h264".
            None skips the check.
        subject: The input, from `input_subject`.
        allowed: The codecs the provider accepts, as ffprobe names them, such as ("h264", "hevc").
            The names messages show ("H.264", "H.265") and "AVC" work too; case doesn't matter.
        docs_url: The provider's page documenting the limit.

    Raises:
        TypeError: If `allowed` is a single string rather than a sequence of codecs.
        ValueError: If `allowed` names a codec this module doesn't know.
    """
    return _codec_error(codec, subject=subject, allowed=allowed, docs_url=docs_url)


def check_audio_codec(
    codec: str | None, *, subject: str, allowed: Sequence[str], docs_url: str | None = None
) -> NodeError | None:
    """An error when an audio input's codec isn't one the provider accepts.

    Args:
        codec: The audio stream's codec as ffprobe names it, from `local_audio_probe`, such as "mp3".
            None skips the check.
        subject: The input, from `input_subject`.
        allowed: The codecs the provider accepts, as ffprobe names them, such as ("mp3", "aac"). A WAV's
            codec is a PCM one, such as "pcm_s16le". The names messages show ("MP3", "AC-3") work too;
            case doesn't matter. A profile such as AAC-LC isn't checked, so list its codec, "aac".
        docs_url: The provider's page documenting the limit.

    Raises:
        TypeError: If `allowed` is a single string rather than a sequence of codecs.
        ValueError: If `allowed` names a codec this module doesn't know.
    """
    return _codec_error(codec, subject=subject, allowed=allowed, docs_url=docs_url)


def check_duration(  # noqa: PLR0913
    seconds: float | None,
    *,
    subject: str,
    minimum: float | None = None,
    maximum: float | None = None,
    fix: str | None = None,
    docs_url: str | None = None,
) -> NodeError | None:
    """An error when an audio or video input is shorter or longer than the provider accepts.

    Args:
        seconds: The input's duration. For a video, pass a `local_video_probe` result's
            `shortest_seconds` for a maximum or `longest_seconds` for a minimum, the readings
            the `Duration` validator uses. None skips the check.
        subject: The input, from `input_subject`.
        minimum: The shortest input accepted, in seconds.
        maximum: The longest input accepted, in seconds.
        fix: What the artist can do about it. Defaults to shortening it or using a longer clip.
        docs_url: The provider's page documenting the limit.
    """
    if seconds is None:
        return None
    if maximum is not None and seconds > maximum:
        next_step = fix or "Shorten it."
        return limit_error(
            f"{subject} is {_format_past(seconds, maximum, 2)} seconds long and the limit is {_format_limit(maximum)}. {next_step}",
            docs_url,
        )
    if minimum is not None and seconds < minimum:
        next_step = fix or "Use a longer clip."
        return limit_error(
            f"{subject} is {_format_past(seconds, minimum, 2)} seconds long and the minimum is {_format_limit(minimum, round_up=True)}. {next_step}",
            docs_url,
        )
    return None


def check_frame_rate(  # noqa: PLR0913
    fps: float | None,
    *,
    subject: str,
    minimum: float | None = None,
    maximum: float | None = None,
    tolerance: float = FRAME_RATE_TOLERANCE,
    docs_url: str | None = None,
) -> NodeError | None:
    """An error when a video's frame rate is outside what the provider accepts.

    Args:
        fps: The frame rate, from `local_video_probe`. None skips the check.
        subject: The input, from `input_subject`.
        minimum: The lowest frame rate accepted.
        maximum: The highest frame rate accepted.
        tolerance: How far past a limit, as a fraction of it, still passes. The default lets NTSC
            footage at 23.976 fps through a "24 fps" minimum and 59.94 fps through a "60 fps" one.
        docs_url: The provider's page documenting the limit.
    """
    if fps is None:
        return None
    if minimum is not None and fps < minimum * (1 - tolerance):
        return limit_error(
            f"{subject} is {_format_past(fps, minimum, 0)} fps and the minimum is "
            f"{_format_limit(minimum, round_up=True)} fps. Re-export it at a higher frame rate.",
            docs_url,
        )
    if maximum is not None and fps > maximum * (1 + tolerance):
        return limit_error(
            f"{subject} is {_format_past(fps, maximum, 0)} fps and the maximum is {_format_limit(maximum)} fps. "
            "Re-export it at a lower frame rate.",
            docs_url,
        )
    return None


def check_frame_count(  # noqa: PLR0913
    frames: int | None,
    *,
    subject: str,
    minimum: int | None = None,
    maximum: int | None = None,
    fix: str | None = None,
    docs_url: str | None = None,
) -> NodeError | None:
    """An error when a video has fewer or more frames than the provider accepts.

    Args:
        frames: The frame count. Pass a `local_video_probe` result's `fewest_frames` for a maximum or
            `most_frames` for a minimum, the readings the `FrameCount` validator uses. None skips
            the check.
        subject: The input, from `input_subject`.
        minimum: The fewest frames accepted.
        maximum: The most frames accepted.
        fix: What the artist can do about too many frames. Defaults to shortening the clip or
            lowering its frame rate.
        docs_url: The provider's page documenting the limit.
    """
    if frames is None:
        return None
    if maximum is not None and frames > maximum:
        return limit_error(
            f"{subject} has {frames:,} frames and the limit is {maximum:,}. "
            f"{fix or 'Shorten it, or lower its frame rate.'}",
            docs_url,
        )
    if minimum is not None and frames < minimum:
        return limit_error(
            f"{subject} has {frames:,} frames and the minimum is {minimum:,}. Use a longer clip.", docs_url
        )
    return None


def _codec_error(codec: str | None, *, subject: str, allowed: Sequence[str], docs_url: str | None) -> NodeError | None:
    """`check_video_codec` and `check_audio_codec`."""
    accepted = _require_known_codecs(allowed)
    # An empty list is no limit, as `check_mime_type` treats it.
    if codec is None or not accepted:
        return None
    if codec.strip().lower() in accepted:
        return None
    codecs = _join_or([_CODEC_NAMES[name] for name in accepted])
    return limit_error(f"{subject} is encoded as {_codec_name(codec)}. Re-encode it as {codecs}.", docs_url)


def _require_known_codecs(allowed: Sequence[str]) -> tuple[str, ...]:
    """The ffprobe names of the codecs `allowed` lists, each once, in order.

    Raises:
        TypeError: If `allowed` is one string rather than a sequence of codecs.
        ValueError: If a name isn't a codec this module knows.
    """
    if isinstance(allowed, str):
        msg = (
            "Attempted to check a codec. Failed because the allowed codecs were one string; "
            "pass a tuple such as ('h264',)."
        )
        raise TypeError(msg)
    accepted: dict[str, None] = {}
    for name in allowed:
        canonical = _canonical_codec(name)
        if canonical is None:
            msg = (
                f"Attempted to check a codec. Failed because {name!r} isn't a codec this module knows; "
                "pass ffprobe's name for it, such as 'h264' or 'pcm_s16le'."
            )
            raise ValueError(msg)
        accepted[canonical] = None
    return tuple(accepted)


def _audio_readings_of(allowed: Sequence[str]) -> tuple[str, ...]:
    """The extra names an audio check matches for the video containers a provider lists.

    A video file's audio reads under its container's audio name (an MP4's as audio/mp4, an MKV's as
    audio/x-matroska), and a provider listing the video container takes that file. The names widen
    the match only; a refusal lists just the names the provider wrote.
    """
    readings = (_VIDEO_TO_AUDIO_CONTAINER.get(_canonical_mime_type(name)) for name in allowed)
    return tuple(reading for reading in readings if reading is not None)


def _is_allowed(mime_type: str, allowed: Sequence[str]) -> bool:
    """Whether a MIME type is one the provider accepts, under any name the provider writes it as.

    Providers and Python's `mimetypes` give some formats other names than the readers return, such
    as "audio/mp3" for "audio/mpeg" or "image/heic" for "image/heif", so both sides are compared by
    their usual name.
    """
    canonical = _canonical_mime_type(mime_type)
    shown = _format_name(mime_type)
    # A name that reads the same as the input's in a message also matches, so a refusal can never
    # name the input's format among the ones it asks for.
    return any(_canonical_mime_type(name) == canonical or _format_name(name) == shown for name in allowed)


def _canonical_mime_type(mime_type: str) -> str:
    """A MIME type's usual name, so its other names, and any capitalisation of them, compare equal to it."""
    # Parameters such as "; codecs=opus" name a codec, which `allowed_codecs` checks, not a format.
    folded = mime_type.split(";", 1)[0].strip().lower()
    return _MIME_ALIASES.get(folded, folded)


def _require_mime_type_sequence(allowed: Sequence[str]) -> None:
    """Raise when `allowed` is one string: it would match by substring and list its letters."""
    if isinstance(allowed, str):
        msg = (
            "Attempted to check a format. Failed because the allowed formats were one string; "
            "pass a tuple such as ('image/png',)."
        )
        raise TypeError(msg)


def _require_valid_exact_ratios(ratios: Sequence[str]) -> None:
    """Raise when `ratios` can't be checked: one string, empty, or holding something that isn't a ratio."""
    if isinstance(ratios, str):
        msg = (
            "Attempted to check an exact aspect ratio. Failed because the ratios were one string; "
            "pass a tuple such as ('16:9', '9:16')."
        )
        raise TypeError(msg)
    if not ratios:
        msg = "Attempted to check an exact aspect ratio. Failed because no ratios were given."
        raise ValueError(msg)
    for name in ratios:
        _parse_ratio(name)


def _format_limit(limit: float, *, round_up: bool = False) -> str:
    """A limit as messages show it: up to 6 significant figures, rounded toward the allowed side.

    A maximum rounds down and a minimum rounds up (`round_up`), so a value past the limit can always
    be printed past the shown limit too.
    """
    text = f"{limit:g}"
    shown = float(text)
    if shown == limit or limit <= 0 or (shown > limit) == round_up:
        return text
    scale = 10 ** max(6 - len(str(int(limit))), 0)
    if round_up:
        rounded = math.ceil(limit * scale)
    else:
        rounded = math.floor(limit * scale)
    return f"{rounded / scale:g}"


def _format_past(value: float, limit: float, decimals: int) -> str:
    """`value` printed past `limit`: to `decimals` places, or more where fewer would not be.

    More places are used when fewer would read as equal to `limit` as `_format_limit` shows it, or
    on its other side. A file of 200,000,001 bytes against a 200,000,000-byte limit (shown as "190.734 MB") must not
    be reported as "190.735 MB" either way round.
    """
    over = value > limit
    shown_limit = float(_format_limit(limit, round_up=not over))
    for places in range(decimals, decimals + 9):
        text = f"{value:.{places}f}"
        rounded = float(text)
        if rounded != shown_limit and (rounded > shown_limit) == over:
            return text
    return repr(value)


# Other names providers and `mimetypes` use for a format, mapped to the one the readers return.
# HEIC and HEIF share one container, which Pillow names HEIF.
_MIME_ALIASES = {
    "image/heic": "image/heif",
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "video/mov": "video/quicktime",
    "video/matroska": "video/x-matroska",
    "video/mkv": "video/x-matroska",
    "audio/matroska": "audio/x-matroska",
    "audio/mka": "audio/x-matroska",
    "audio/mpeg3": "audio/mpeg",
    "audio/x-mpeg-3": "audio/mpeg",
    "video/avi": "video/x-msvideo",
    "video/msvideo": "video/x-msvideo",
    "video/wmv": "video/x-ms-wmv",
    "video/mpg": "video/mpeg",
    "audio/mp3": "audio/mpeg",
    "audio/x-mpeg": "audio/mpeg",
    "audio/x-wav": "audio/wav",
    "audio/wave": "audio/wav",
    "audio/vnd.wave": "audio/wav",
    "audio/x-m4a": "audio/mp4",
    "audio/m4a": "audio/mp4",
    "audio/x-flac": "audio/flac",
    "audio/x-aiff": "audio/aiff",
    "audio/x-aac": "audio/aac",
    "audio/x-ogg": "audio/ogg",
    # A .opus file is Ogg. Listing only audio/opus then also accepts Ogg Vorbis, which refuses less.
    "audio/opus": "audio/ogg",
    "audio/aif": "audio/aiff",
    "audio/x-aif": "audio/aiff",
    "audio/mp4a-latm": "audio/mp4",
    "audio/x-mp4": "audio/mp4",
}


# The audio reading of a video container, for an audio check of a video file's audio track.
_VIDEO_TO_AUDIO_CONTAINER = {
    "video/mp4": "audio/mp4",
    "video/x-matroska": "audio/x-matroska",
    "video/webm": "audio/webm",
    "video/3gpp": "audio/3gpp",
    "video/3gpp2": "audio/3gpp2",
}


# How a message names the input's own format where that differs from how a provider's list reads.
_INPUT_FORMAT_NAMES = {"image/heif": "HEIC"}


# Formats whose MIME subtype isn't the name an artist knows them by.
_FORMAT_NAMES = {
    "image/vnd.adobe.photoshop": "PSD",
    "image/x-portable-anymap": "PPM",
    "application/postscript": "EPS",
    "video/quicktime": "MOV",
    "video/x-matroska": "MKV",
    "video/x-msvideo": "AVI",
    "video/avi": "AVI",
    "video/msvideo": "AVI",
    "video/mov": "MOV",
    "video/matroska": "MKV",
    "video/mkv": "MKV",
    "audio/matroska": "MKA",
    "audio/mka": "MKA",
    "audio/mpeg3": "MP3",
    "audio/x-mpeg-3": "MP3",
    "image/jpg": "JPEG",
    "image/pjpeg": "JPEG",
    "video/mpg": "MPEG",
    "video/wmv": "WMV",
    "audio/x-ogg": "OGG",
    "audio/opus": "Opus",
    "audio/aif": "AIFF",
    "audio/x-aif": "AIFF",
    "audio/mp4a-latm": "M4A",
    "audio/x-mp4": "M4A",
    "audio/x-matroska": "MKA",
    "audio/x-ms-wma": "WMA",
    "audio/x-caf": "CAF",
    "audio/basic": "AU",
    "audio/ac3": "AC-3",
    "application/mxf": "MXF",
    "video/dv": "DV",
    "video/ogg": "OGG",
    "audio/3gpp": "3GP",
    "audio/3gpp2": "3G2",
    "video/webm": "WebM",
    "video/3gpp": "3GP",
    "video/3gpp2": "3G2",
    "video/mp2t": "MPEG-TS",
    "video/x-ms-wmv": "WMV",
    "audio/mpeg": "MP3",
    "audio/mp4": "M4A",
    "audio/wav": "WAV",
    "audio/x-wav": "WAV",
    "audio/wave": "WAV",
    "audio/vnd.wave": "WAV",
    "audio/mp3": "MP3",
    "audio/x-mpeg": "MP3",
    "audio/x-m4a": "M4A",
    "audio/m4a": "M4A",
    "audio/ogg": "OGG",
    "audio/webm": "WebM",
    "audio/aiff": "AIFF",
}


# ffprobe's codec names that aren't the name an artist knows a codec by.
_CODEC_NAMES = {
    "h264": "H.264",
    "hevc": "H.265",
    "av1": "AV1",
    "vp8": "VP8",
    "vp9": "VP9",
    "prores": "ProRes",
    "mpeg4": "MPEG-4",
    "mpeg2video": "MPEG-2",
    "mjpeg": "Motion JPEG",
    "dnxhd": "DNxHD",
    "mp3": "MP3",
    "aac": "AAC",
    "ac3": "AC-3",
    "eac3": "E-AC-3",
    "flac": "FLAC",
    "opus": "Opus",
    "vorbis": "Vorbis",
    "alac": "ALAC",
    "pcm_s16le": "16-bit PCM (little-endian)",
    "pcm_s24le": "24-bit PCM (little-endian)",
    "pcm_s32le": "32-bit PCM (little-endian)",
    "pcm_f32le": "32-bit float PCM (little-endian)",
    "pcm_s16be": "16-bit PCM (big-endian)",
    "pcm_s24be": "24-bit PCM (big-endian)",
    "pcm_s32be": "32-bit PCM (big-endian)",
    "pcm_f32be": "32-bit float PCM (big-endian)",
    "pcm_u8": "8-bit PCM",
    "pcm_mulaw": "G.711 μ-law",
    "amr_nb": "AMR-NB",
    "amr_wb": "AMR-WB",
    "speex": "Speex",
    "h263": "H.263",
    "pcm_alaw": "G.711 A-law",
}


# A codec as messages show it, lowercased, mapped to ffprobe's name, so a library may write "H.264".
_CODECS_BY_SHOWN_NAME = {shown.lower(): codec for codec, shown in _CODEC_NAMES.items()}


# Other names a library may write for a codec, mapped to ffprobe's name.
_CODEC_ALIASES = {"avc": "h264", "h265": "hevc"}


def _canonical_codec(name: str) -> str | None:
    """A codec's ffprobe name from the name a library wrote ("h264", "H.264" or "AVC"), or None if unknown."""
    folded = name.strip().lower()
    if folded in _CODEC_NAMES:
        return folded
    return _CODEC_ALIASES.get(folded) or _CODECS_BY_SHOWN_NAME.get(folded)


def _codec_name(codec: str) -> str:
    """A codec ffprobe read as a message shows it: "hevc" reads "H.265", and an unlisted "theora" "THEORA"."""
    return _CODEC_NAMES.get(codec) or codec.upper()


def _format_name(mime_type: str) -> str:
    """A MIME type's format as a message shows it: "image/x-icon" reads "ICON"."""
    folded = mime_type.split(";", 1)[0].strip().lower()
    known = _FORMAT_NAMES.get(folded)
    if known:
        return known
    return folded.split("/", 1)[-1].removeprefix("x-").removeprefix("ffprobe-").removeprefix("vnd.").upper()


def _parse_ratio(name: str) -> float:
    """A ratio written the way providers write it, such as "16:9", as width over height.

    Raises:
        ValueError: If `name` isn't two positive numbers joined by a colon.
    """
    width, separator, height = name.partition(":")
    msg = f"Attempted to check an aspect ratio. Failed because {name!r} is not a ratio such as '16:9'."
    if not separator:
        raise ValueError(msg)
    try:
        width_value = float(width)
        height_value = float(height)
    except ValueError as e:
        raise ValueError(msg) from e
    if not (math.isfinite(width_value) and math.isfinite(height_value)):
        raise ValueError(msg)
    if not width_value > 0 or not height_value > 0:
        raise ValueError(msg)
    return width_value / height_value


def _join_or(names: Sequence[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} or {names[-1]}"
