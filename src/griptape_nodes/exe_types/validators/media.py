"""Limits on an image, video or audio input, declared on its parameter with `pre_run_validators=`.

Each validator wraps one check from `griptape_nodes.exe_types.input_checks`, so its message is that
check's. Like those checks, a validator skips anything the engine couldn't read locally, such as a
remote URL, and leaves it for the provider.

A validator whose limit fits more than one kind of media (`AllowedFormats`, `SideRange`,
`AspectRatioRange` and the rest) reads the kind the parameter holds: an image parameter's image, a
video parameter's video. Arguments that can't be checked, such as a ratio that isn't one or a codec
name nothing here knows, raise when the validator is built rather than when a node runs.

```python
MB = 1024 * 1024

ParameterImage(
    name="image",
    pre_run_validators=[
        AllowedFormats("image/jpeg", "image/png"),
        MaxFileSize(10 * MB),
        SideRange(min=300),
        AspectRatioRange("1:2.5", "2.5:1"),
    ],
)
```
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, override

from griptape_nodes.exe_types.input_checks import (
    FRAME_RATE_TOLERANCE,
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
from griptape_nodes.exe_types.validators.base import EMPTY_MEDIA, MediaInfo, MediaNeed, PreRunValidator

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.exe_types.input_checks import ImageInfo
    from griptape_nodes.exe_types.node_error import NodeError

__all__ = [
    "AllowedFormats",
    "AspectRatioRange",
    "AudioCodecs",
    "Duration",
    "ExactAspectRatios",
    "FrameCount",
    "FrameRate",
    "MaxFileSize",
    "PixelArea",
    "SideRange",
    "VideoCodecs",
]

_FRAME_NEEDS = frozenset({MediaNeed.IMAGE, MediaNeed.VIDEO})
_LENGTH_NEEDS = frozenset({MediaNeed.VIDEO, MediaNeed.AUDIO})

# What `Duration` suggests for audio over its maximum; `check_duration`'s default suggests a video node.
_AUDIO_TOO_LONG_FIX = "Trim it in an audio editor."

# A subject for checking a validator's own arguments when it is built, before there is an input.
_NO_SUBJECT = ""


@dataclass(frozen=True)
class MaxFileSize(PreRunValidator):
    """The largest file the provider accepts, in bytes.

    Args:
        maximum_bytes: The largest file accepted, or with `strict`, the size every file must be under.
        strict: For a provider that writes "less than 30 MB": a file of exactly `maximum_bytes` is
            refused too.
        fix: What the artist can do about it. Defaults to `check_size`'s.
    """

    needs = frozenset({MediaNeed.SIZE})

    maximum_bytes: int
    strict: bool = False
    fix: str | None = None

    @override
    def check(
        self, value: Any, *, subject: str, docs_url: str | None, media: MediaInfo = EMPTY_MEDIA
    ) -> NodeError | None:
        return check_size(
            media.size_bytes,
            maximum_bytes=self.maximum_bytes,
            subject=subject,
            fix=self.fix,
            strict=self.strict,
            docs_url=docs_url,
        )


@dataclass(frozen=True, init=False)
class AllowedFormats(PreRunValidator):
    """The formats the provider accepts, as MIME types, such as `AllowedFormats("image/jpeg", "image/png")`.

    The format is read from the file, not its name, so a MOV renamed to .mp4 is still a MOV. On an
    audio parameter, a video container the provider lists also accepts the audio read from that
    container, such as an MP4's.

    Raises:
        TypeError: If a format isn't a string, such as a tuple passed as one argument.
    """

    needs = frozenset({MediaNeed.IMAGE, MediaNeed.VIDEO, MediaNeed.AUDIO})

    mime_types: tuple[str, ...]

    def __init__(self, *mime_types: str) -> None:
        _require_names(mime_types, what="format", example="'image/png'")
        object.__setattr__(self, "mime_types", mime_types)

    @override
    def check(
        self, value: Any, *, subject: str, docs_url: str | None, media: MediaInfo = EMPTY_MEDIA
    ) -> NodeError | None:
        if media.audio is not None:
            return check_mime_type(
                media.audio.mime_type, subject=subject, allowed=self.mime_types, docs_url=docs_url, audio=True
            )
        return check_mime_type(media.mime_type, subject=subject, allowed=self.mime_types, docs_url=docs_url)


@dataclass(frozen=True, kw_only=True)
class SideRange(PreRunValidator):
    """The fewest and most pixels accepted for an image's or video frame's width and height.

    A video frame is measured as stored, turned upright for a rotated clip.
    """

    needs = _FRAME_NEEDS

    min: int | None = None
    max: int | None = None

    @override
    def check(
        self, value: Any, *, subject: str, docs_url: str | None, media: MediaInfo = EMPTY_MEDIA
    ) -> NodeError | None:
        return check_dimensions(media.frame, subject=subject, min_side=self.min, max_side=self.max, docs_url=docs_url)


@dataclass(frozen=True, kw_only=True)
class PixelArea(PreRunValidator):
    """The fewest and most pixels accepted in an image or video frame, in millions."""

    needs = _FRAME_NEEDS

    min_megapixels: float | None = None
    max_megapixels: float | None = None

    @override
    def check(
        self, value: Any, *, subject: str, docs_url: str | None, media: MediaInfo = EMPTY_MEDIA
    ) -> NodeError | None:
        return check_dimensions(
            media.frame,
            subject=subject,
            min_megapixels=self.min_megapixels,
            max_megapixels=self.max_megapixels,
            docs_url=docs_url,
        )


@dataclass(frozen=True)
class AspectRatioRange(PreRunValidator):
    """The tallest and widest aspect ratios accepted, as the provider writes them, such as ("1:3", "3:1").

    A video whose pixels aren't square passes when either its stored or its displayed shape fits.

    Raises:
        ValueError: If `tallest` or `widest` isn't a ratio such as "16:9", or `tallest` is the wider
            of the two.
    """

    needs = _FRAME_NEEDS

    tallest: str
    widest: str

    def __post_init__(self) -> None:
        check_aspect_ratio_range(None, subject=_NO_SUBJECT, widest=self.widest, tallest=self.tallest)

    @override
    def check(
        self, value: Any, *, subject: str, docs_url: str | None, media: MediaInfo = EMPTY_MEDIA
    ) -> NodeError | None:
        return _easier_shape(
            media,
            lambda frame: check_aspect_ratio_range(
                frame, subject=subject, widest=self.widest, tallest=self.tallest, docs_url=docs_url
            ),
        )


@dataclass(frozen=True, init=False)
class ExactAspectRatios(PreRunValidator):
    """The only aspect ratios accepted, such as `ExactAspectRatios("16:9", "9:16")`.

    A frame matches when a side is within a pixel of the ratio. A video whose pixels aren't square
    passes when either its stored or its displayed shape matches.

    Raises:
        ValueError: If no ratio is given or one isn't a ratio such as "16:9".
        TypeError: If a ratio isn't a string, such as a tuple passed as one argument.
    """

    needs = _FRAME_NEEDS

    ratios: tuple[str, ...]

    def __init__(self, *ratios: str) -> None:
        _require_names(ratios, what="aspect ratio", example="'16:9'")
        check_aspect_ratio_exact(None, subject=_NO_SUBJECT, ratios=ratios)
        object.__setattr__(self, "ratios", ratios)

    @override
    def check(
        self, value: Any, *, subject: str, docs_url: str | None, media: MediaInfo = EMPTY_MEDIA
    ) -> NodeError | None:
        return _easier_shape(
            media,
            lambda frame: check_aspect_ratio_exact(frame, subject=subject, ratios=self.ratios, docs_url=docs_url),
        )


@dataclass(frozen=True, kw_only=True)
class Duration(PreRunValidator):
    """The shortest and longest video or audio input accepted, in seconds.

    Providers don't say whether they measure the stream or the whole file, so a maximum is checked
    against the shorter of the two and a minimum against the longer.

    Args:
        min: The shortest input accepted.
        max: The longest input accepted.
        fix: What the artist can do about an input of the wrong length. Defaults to trimming it (in
            a Trim Video node for a video, an audio editor for audio) or using a longer clip.
    """

    needs = _LENGTH_NEEDS

    min: float | None = None
    max: float | None = None
    fix: str | None = None

    @override
    def check(
        self, value: Any, *, subject: str, docs_url: str | None, media: MediaInfo = EMPTY_MEDIA
    ) -> NodeError | None:
        probe = media.video
        too_long_fix = self.fix
        if probe is None:
            probe = media.audio
            if too_long_fix is None:
                too_long_fix = _AUDIO_TOO_LONG_FIX
        if probe is None:
            return None
        if self.max is not None:
            error = check_duration(
                probe.shortest_seconds, subject=subject, maximum=self.max, fix=too_long_fix, docs_url=docs_url
            )
            if error is not None:
                return error
        if self.min is None:
            return None
        return check_duration(probe.longest_seconds, subject=subject, minimum=self.min, fix=self.fix, docs_url=docs_url)


@dataclass(frozen=True, kw_only=True)
class FrameRate(PreRunValidator):
    """The lowest and highest frame rates accepted for a video.

    A variable-frame-rate clip's average and declared rates differ, so each bound is checked against
    the one easier to meet. 1% leeway lets 23.976 fps through a 24 fps minimum.
    """

    needs = frozenset({MediaNeed.VIDEO})

    min: float | None = None
    max: float | None = None
    tolerance: float = FRAME_RATE_TOLERANCE

    @override
    def check(
        self, value: Any, *, subject: str, docs_url: str | None, media: MediaInfo = EMPTY_MEDIA
    ) -> NodeError | None:
        if media.video is None:
            return None
        rates = [rate for rate in (media.video.fps, media.video.nominal_fps) if rate is not None]
        if self.max is not None:
            error = check_frame_rate(
                min(rates, default=None),
                subject=subject,
                maximum=self.max,
                tolerance=self.tolerance,
                docs_url=docs_url,
            )
            if error is not None:
                return error
        if self.min is None:
            return None
        return check_frame_rate(
            max(rates, default=None), subject=subject, minimum=self.min, tolerance=self.tolerance, docs_url=docs_url
        )


@dataclass(frozen=True, kw_only=True)
class FrameCount(PreRunValidator):
    """The fewest and most frames accepted for a video.

    A file that records no count, such as an MKV, is estimated from its durations and frame rates,
    each bound against the estimate easier to meet.
    """

    needs = frozenset({MediaNeed.VIDEO})

    min: int | None = None
    max: int | None = None

    @override
    def check(
        self, value: Any, *, subject: str, docs_url: str | None, media: MediaInfo = EMPTY_MEDIA
    ) -> NodeError | None:
        if media.video is None:
            return None
        if self.max is not None:
            error = check_frame_count(media.video.fewest_frames, subject=subject, maximum=self.max, docs_url=docs_url)
            if error is not None:
                return error
        if self.min is None:
            return None
        return check_frame_count(media.video.most_frames, subject=subject, minimum=self.min, docs_url=docs_url)


@dataclass(frozen=True, init=False)
class VideoCodecs(PreRunValidator):
    """The video codecs accepted, as ffprobe names them, such as `VideoCodecs("h264", "hevc")`.

    The names messages show ("H.264", "H.265") and "AVC" work too.

    Raises:
        TypeError: If a codec isn't a string, such as a tuple passed as one argument.
        ValueError: If a codec isn't one `input_checks` knows.
    """

    needs = frozenset({MediaNeed.VIDEO})

    codecs: tuple[str, ...]

    def __init__(self, *codecs: str) -> None:
        _require_names(codecs, what="codec", example="'h264'")
        check_video_codec(None, subject=_NO_SUBJECT, allowed=codecs)
        object.__setattr__(self, "codecs", codecs)

    @override
    def check(
        self, value: Any, *, subject: str, docs_url: str | None, media: MediaInfo = EMPTY_MEDIA
    ) -> NodeError | None:
        if media.video is None:
            return None
        return check_video_codec(media.video.codec, subject=subject, allowed=self.codecs, docs_url=docs_url)


@dataclass(frozen=True, init=False)
class AudioCodecs(PreRunValidator):
    """The audio codecs accepted, as ffprobe names them, such as `AudioCodecs("mp3", "pcm_s16le")`.

    A WAV's codec is a PCM one, such as "pcm_s16le". The names messages show ("MP3", "AC-3") work too.

    Raises:
        TypeError: If a codec isn't a string, such as a tuple passed as one argument.
        ValueError: If a codec isn't one `input_checks` knows.
    """

    needs = frozenset({MediaNeed.AUDIO})

    codecs: tuple[str, ...]

    def __init__(self, *codecs: str) -> None:
        _require_names(codecs, what="codec", example="'mp3'")
        check_audio_codec(None, subject=_NO_SUBJECT, allowed=codecs)
        object.__setattr__(self, "codecs", codecs)

    @override
    def check(
        self, value: Any, *, subject: str, docs_url: str | None, media: MediaInfo = EMPTY_MEDIA
    ) -> NodeError | None:
        if media.audio is None:
            return None
        return check_audio_codec(media.audio.codec, subject=subject, allowed=self.codecs, docs_url=docs_url)


def _easier_shape(media: MediaInfo, check: Callable[[ImageInfo | None], NodeError | None]) -> NodeError | None:
    """A shape check's error for the stored frame, or None when the displayed frame passes it.

    Providers don't say whether they measure a frame's stored or displayed shape, so a shape check
    passes when either does.
    """
    error = check(media.frame)
    display_frame = media.display_frame
    if error is None or display_frame is None:
        return error
    if check(display_frame) is None:
        return None
    return error


def _require_names(names: tuple[Any, ...], *, what: str, example: str) -> None:
    """Raise when a validator listing names was given something other than strings."""
    for name in names:
        if not isinstance(name, str):
            msg = (
                f"Attempted to list an allowed {what}. Failed because {name!r} isn't a string; "
                f"pass each one as its own argument, such as {example}."
            )
            raise TypeError(msg)
