"""Reading a media input's properties locally, for the pre-run validators and a node's own checks.

A request a provider is certain to reject still costs the artist an upload and a wait, and sometimes
credits. A node declares the limits on its parameters with `pre_run_validators=` (see
`griptape_nodes.exe_types.validators`), and the engine checks them at the node's turn, before it
runs. This module reads what they check, and a custom validator or a node's own
`validate_before_node_run` can read with it too:

- **Readers** (`local_size_bytes`, `local_image_info`, `local_image_format`, `local_video_probe`,
  `local_audio_probe`) read a media input's properties from data already on this machine. `local_video_probe` reads a
  video's container, codec, frame size, durations and frame rates in one ffprobe run, and
  `local_audio_probe` an audio input's container, codec and durations. None of them makes a network
  call or downloads a tool: the video and audio readers use ffprobe only if it is already
  installed. They return None when the value is a remote URL, can't be found or parsed, or when the
  tool to read it is missing or too slow. Inside `input_check_readings`, which the engine opens
  around each node's checks, each reader reads a location once and returns the same result after.
  A probe that runs out of time is logged as a warning and skipped, like any other input that
  can't be read here.
- **`read_media`** takes the readings a set of validators needs from one value, in one call.

The validators in `griptape_nodes.exe_types.validators` compare these readings with a provider's
limits and write the messages.

A limit is enforced only where it can be read cheaply; anything else is left for the provider.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import math
import shutil
import subprocess
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from PIL import Image, UnidentifiedImageError
from static_ffmpeg import run as static_ffmpeg_run

from griptape_nodes.exe_types.validators.base import MediaInfo, MediaNeed
from griptape_nodes.files.file import File, FileLoadError
from griptape_nodes.files.path_utils import is_url
from griptape_nodes.utils.media_values import MediaKind, coerce_media_url_or_data_uri

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterator

logger = logging.getLogger("griptape_nodes")

__all__ = [
    "AudioProbe",
    "ImageInfo",
    "VideoProbe",
    "input_check_readings",
    "input_subject",
    "local_audio_probe",
    "local_image_format",
    "local_image_info",
    "local_size_bytes",
    "local_video_probe",
    "read_media",
]


# A local probe takes tens of milliseconds. Validation runs in a worker thread, so a slow probe holds
# up only its own node; this only stops a hung ffprobe (a stalled network drive, a damaged file). A
# probe that takes longer is abandoned and the checks that need it (container, codec, frame size,
# duration, frame rate) are skipped.
FFPROBE_TIMEOUT_SECONDS = 10

# An ISO media file (MP4, MOV, HEIC, AVIF) opens with an `ftyp` box: a 4-byte size, "ftyp", then
# the 4-byte major brand.
_ISO_FTYP_TYPE = b"ftyp"
_ISO_FTYP_HEADER_BYTES = 12
# Enough base64 for a data URI's first `_ISO_FTYP_HEADER_BYTES` bytes.
_ISO_FTYP_HEADER_BASE64_CHARS = 16

# EXIF Orientation tag (0x0112) values that display an image rotated a quarter turn.
_EXIF_ORIENTATION_TAG = 0x0112
_QUARTER_TURN_ORIENTATIONS = frozenset({5, 6, 7, 8})

_QUARTER_TURN_DEGREES = 90
_HALF_TURN_DEGREES = 180

# What the readers have read in the current `input_check_readings` block, by reader and location, or
# None outside one. A reading that came back None is kept too, so an unreadable input isn't retried.
_readings: ContextVar[dict[tuple[MediaNeed, str], Any] | None] = ContextVar("input_check_readings", default=None)


class ImageInfo(NamedTuple):
    """An image's or video frame's pixel size, and an image's MIME type read from its header."""

    width: int
    height: int
    mime_type: str | None


def input_subject(parameter: str, *, noun: str, position: int | None = None) -> str:
    """The start of a message naming one input, such as "Image 2 in 'reference images'".

    Args:
        parameter: The parameter's name as the UI shows it.
        noun: What the input is, such as "image" or "video".
        position: The input's 1-based position in a list parameter, or None for a single input.
    """
    if position is None:
        return f"The {noun} in '{parameter}'"
    return f"{noun[:1].upper()}{noun[1:]} {position} in '{parameter}'"


@contextmanager
def input_check_readings() -> Iterator[None]:
    """Read each media location at most once per reader inside this block.

    The engine opens one around each node's pre-run checks, so the validators on a parameter and
    the node's own `validate_before_node_run` share one read of each input. What
    was read is dropped when the block ends, so nothing carries over to the next run or another node.
    Opened inside another one, it shares the outer block's readings.
    """
    if _readings.get() is not None:
        yield
        return
    token = _readings.set({})
    try:
        yield
    finally:
        _readings.reset(token)


def read_media(value: Any, needs: Collection[MediaNeed], *, kind: MediaKind) -> MediaInfo:
    """The readings `needs` names, of one media input, taken once.

    Reduces the input to one location and hands that to each reader asked for: `local_size_bytes`
    for `SIZE`, `local_image_info` for `IMAGE`, `local_video_probe` for `VIDEO` and
    `local_audio_probe` for `AUDIO`. A reading not asked for, or that couldn't be taken, is None.
    An image Pillow can't open, such as a HEIC photo, still has its format read from its file
    signature where that names one, so a format check can judge it while the size checks skip.

    Args:
        value: The media input: an artifact, a path, a URL or a data URI.
        needs: The readings to take.
        kind: The kind of media the input's parameter holds, which says how to reduce it to a location.
    """
    location = coerce_media_url_or_data_uri(value, kind=kind)
    if not location:
        return MediaInfo()
    size_bytes = None
    image = None
    image_format = None
    video = None
    audio = None
    if MediaNeed.SIZE in needs:
        size_bytes = local_size_bytes(location, kind=kind)
    if MediaNeed.IMAGE in needs:
        image = local_image_info(location)
        if image is None:
            image_format = _signature_image_format(location)
    if MediaNeed.VIDEO in needs:
        video = local_video_probe(location)
    if MediaNeed.AUDIO in needs:
        audio = local_audio_probe(location)
    return MediaInfo(
        size_bytes=size_bytes,
        image=image,
        image_format=image_format,
        video=video,
        audio=audio,
    )


def _read_once(reader: MediaNeed, location: str, read: Callable[[str], Any]) -> Any:
    """`read(location)`, or what it returned before in the current `input_check_readings` block."""
    readings = _readings.get()
    if readings is None:
        return read(location)
    key = (reader, location)
    if key not in readings:
        readings[key] = read(location)
    return readings[key]


def local_size_bytes(value: Any, *, kind: MediaKind) -> int | None:
    """The size in bytes of a media input, or None if it can't be read locally.

    A data URI is measured from its base64 length. A path, a macro path, or a localhost static file
    URL is measured on disk. A remote URL returns None rather than being fetched.
    """
    location = coerce_media_url_or_data_uri(value, kind=kind)
    if not location:
        return None
    return _read_once(MediaNeed.SIZE, location, _read_size_bytes)


def _read_size_bytes(location: str) -> int | None:
    """`local_size_bytes` for a location, without the per-pass cache."""
    if location.startswith("data:"):
        payload = "".join(location.split(",", 1)[-1].split())
        return len(payload) * 3 // 4 - payload[-2:].count("=")
    path = _local_path(location)
    if path is None:
        return None
    try:
        return path.stat().st_size
    except OSError:
        return None


def local_image_info(value: Any) -> ImageInfo | None:
    """The pixel size and MIME type of an image input, or None if it can't be read locally.

    Reads only the image header (Pillow reads a WebP or AVIF file in full, without decoding it), and
    reports the size as displayed, so a phone photo stored landscape with an EXIF rotation reads as
    portrait. Returns None for a remote URL, for a format Pillow can't open (a RAW camera file or a
    HEIC photo, say; `local_image_format` still reads a HEIC's format), and for an image over
    Pillow's decompression-bomb limit (about 179 megapixels), which Pillow refuses to open
    without decoding anything. The checks that use it then skip, rather than reject a valid image or
    override a process-wide Pillow setting.
    """
    location = coerce_media_url_or_data_uri(value, kind="image")
    if not location:
        return None
    return _read_once(MediaNeed.IMAGE, location, _read_image_info)


def local_image_format(value: Any) -> str | None:
    """The MIME type of an image input, read from the file rather than its name, or None if unknown.

    Pillow's reading (see `local_image_info`), or for an image Pillow can't open, the format its
    file signature names. That reads HEIC and HEIF photos, which Pillow can't open without a plugin,
    as "image/heif", so a format check accepts them where a provider lists either name and refuses
    them, named as HEIC, where it doesn't. Returns None for a remote URL and for a file whose
    signature names no image format.
    """
    location = coerce_media_url_or_data_uri(value, kind="image")
    if not location:
        return None
    info = local_image_info(location)
    if info is not None:
        return info.mime_type
    return _signature_image_format(location)


def _signature_image_format(location: str) -> str | None:
    """The image format an ISO media file's major brand names, such as "image/heif" for a HEIC photo.

    Reads only the file's first 12 bytes, and None unless they hold an `ftyp` box whose brand names
    a still image; an MP4 or MOV is a video, for the video reader to judge.
    """
    header = _file_header(location, _ISO_FTYP_HEADER_BYTES)
    if header is None or len(header) < _ISO_FTYP_HEADER_BYTES or header[4:8] != _ISO_FTYP_TYPE:
        return None
    brand = header[8:12].decode("ascii", errors="replace")
    mime_type = _iso_media_mime_type(brand)
    if not mime_type.startswith("image/"):
        return None
    return mime_type


def _file_header(location: str, size: int) -> bytes | None:
    """The first `size` bytes of a data URI's payload or a file on this machine, or None."""
    if location.startswith("data:"):
        payload = "".join(location.split(",", 1)[-1].split())
        try:
            return base64.b64decode(payload[:_ISO_FTYP_HEADER_BASE64_CHARS], validate=True)[:size]
        except (binascii.Error, ValueError) as e:
            logger.debug("Could not read an image's signature for a pre-run input check, skipping it: %s", e)
            return None
    path = _local_path(location)
    if path is None:
        return None
    try:
        with path.open("rb") as file:
            return file.read(size)
    except OSError as e:
        logger.debug("Could not read an image's signature for a pre-run input check, skipping it: %s", e)
        return None


def _read_image_info(location: str) -> ImageInfo | None:
    """`local_image_info` for a location, without the per-pass cache."""
    source = _image_source(location)
    if source is None:
        return None
    try:
        with Image.open(source) as image:
            image_format = image.format
            width = image.width
            height = image.height
            orientation = _exif_orientation(image)
    # Pillow re-raises NotImplementedError from a format plugin that recognises a file but can't decode
    # it, such as a DDS texture in an unsupported DXGI format. Its AVIF plugin passes on libavif's
    # RuntimeError for a damaged file.
    except (
        OSError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        ValueError,
        NotImplementedError,
        RuntimeError,
    ) as e:
        logger.debug("Could not read image header for a pre-run input check, skipping it: %s", e)
        return None
    # Phones store a portrait shot landscape with an EXIF orientation of 5 to 8, which displays it a
    # quarter turn round; report the size as displayed, as the video reader does.
    if orientation in _QUARTER_TURN_ORIENTATIONS:
        width, height = height, width
    # Phone and camera JPEGs with Multi-Picture extensions open as MPO; they are JPEG streams.
    if image_format == "MPO":
        mime_type = "image/jpeg"
    else:
        mime_type = Image.MIME.get(image_format or "")
    # Pillow registers no MIME type for some formats it reads, such as DDS and QOI. Name one after
    # its format so a format check can still refuse it, rather than skipping a format it has read.
    if mime_type is None and image_format:
        mime_type = f"image/{image_format.lower()}"
    return ImageInfo(width=width, height=height, mime_type=mime_type)


def _exif_orientation(image: Image.Image) -> int | None:
    """The EXIF Orientation tag of an image Pillow reports at its stored size, read from its header.

    None if it has none, if it can't be read, or if Pillow already reports the displayed size: its
    TIFF plugin applies the orientation itself, so applying it again would turn the image back.
    Pillow's PNG plugin decodes every pixel to look for an eXIf chunk after the image data, so a PNG
    is read only when its header already held one. An eXIf chunk after the data is rare, and such a
    PNG keeps its stored size.
    """
    if image.format == "TIFF":
        return None
    if image.format == "PNG" and "exif" not in image.info:
        return None
    try:
        orientation = image.getexif().get(_EXIF_ORIENTATION_TAG)
    except (OSError, ValueError, SyntaxError) as e:
        logger.debug("Could not read an image's EXIF orientation for a pre-run input check: %s", e)
        return None
    if isinstance(orientation, int):
        return orientation
    return None


class VideoProbe(NamedTuple):
    """A video's frame size (as stored, turned upright), duration and frame rate, read with one ffprobe run.

    `seconds` is the video stream's duration, falling back to the container's. The container's
    duration covers every stream, so a longer audio track can make it longer than the video;
    `container_seconds` keeps it so a minimum can be checked against the longer of the two.
    `fps` is the average frame rate and `nominal_fps` the stream's declared one; they differ only
    for a variable-frame-rate clip, so each frame-rate bound is checked against the easier of them.
    `mime_type` is the container, such as "video/mp4" or "video/quicktime", read from the file rather
    than its name, and `codec` the video stream's, as ffprobe names it, such as "h264" or "hevc".
    `display_info` is the frame's shape as it plays when its pixels aren't square (anamorphic footage
    such as 1440x1080 shown at 16:9), and None when they are, so `info` already gives the shape.
    `frames` is the frame count the file records; MP4, MOV and AVI record one, MKV and WebM don't.
    Each part is None when ffprobe didn't report it.
    """

    info: ImageInfo | None
    seconds: float | None
    fps: float | None = None
    container_seconds: float | None = None
    nominal_fps: float | None = None
    mime_type: str | None = None
    codec: str | None = None
    display_info: ImageInfo | None = None
    frames: int | None = None

    @property
    def fewest_frames(self) -> int | None:
        """The frame count a maximum is checked against: the recorded count, or the lowest estimate.

        Without a recorded count, the count is estimated from each duration and frame rate the probe
        read, rounding down, as providers do when they write "73 frames (around 3 seconds at 24fps)".
        """
        if self.frames is not None:
            return self.frames
        return min(self._frame_estimates(), default=None)

    @property
    def most_frames(self) -> int | None:
        """The frame count a minimum is checked against: the recorded count, or the highest estimate."""
        if self.frames is not None:
            return self.frames
        return max(self._frame_estimates(), default=None)

    def _frame_estimates(self) -> list[int]:
        rates = [rate for rate in (self.fps, self.nominal_fps) if rate is not None]
        # A small allowance, so 2.9999 s at 24 fps reads as the 72 frames it holds rather than 71.
        return [math.floor(seconds * rate + 1e-6) for seconds in self._durations() for rate in rates]

    @property
    def shortest_seconds(self) -> float | None:
        """The shorter of the stream and container durations, or None if ffprobe reported neither.

        Providers don't say which of the two they measure, so a maximum is checked against this one.
        """
        return min(self._durations(), default=None)

    @property
    def longest_seconds(self) -> float | None:
        """The longer of the stream and container durations, or None if ffprobe reported neither.

        A minimum is checked against this one.
        """
        return max(self._durations(), default=None)

    def _durations(self) -> list[float]:
        return [s for s in (self.seconds, self.container_seconds) if s is not None]


def local_video_probe(value: Any) -> VideoProbe | None:
    """A video input's container, codec, frame size, duration and frame rate, or None if unreadable here.

    One ffprobe run reads them all, so a node checking several on the same clip waits on at most
    one `FFPROBE_TIMEOUT_SECONDS`, and the values come from the same read of the file.
    Probes only a file on this machine, with an ffprobe that is already installed; see
    `_run_ffprobe`.
    """
    location = coerce_media_url_or_data_uri(value, kind="video")
    if not location:
        return None
    return _read_once(MediaNeed.VIDEO, location, _probe_video)


def _probe_video(location: str) -> VideoProbe | None:
    """`local_video_probe` for a location, without the per-pass cache."""
    data = _run_ffprobe(location, ["-show_format", "-select_streams", "v:0", "-show_streams"], kind="video")
    if data is None:
        return None
    stream_seconds = _stream_seconds(_first_stream(data))
    container_seconds = _seconds(data.get("format", {}).get("duration"))
    seconds = stream_seconds
    if seconds is None:
        seconds = container_seconds
    return VideoProbe(
        info=_frame_size(data),
        seconds=seconds,
        fps=_frame_rate(data),
        container_seconds=container_seconds,
        nominal_fps=_nominal_frame_rate(data),
        mime_type=_video_mime_type(data),
        codec=_video_codec(data),
        display_info=_display_frame_size(data),
        frames=_recorded_frames(data),
    )


class AudioProbe(NamedTuple):
    """An audio input's container, codec and duration, read with one ffprobe run.

    `mime_type` is the container, such as "audio/mpeg" or "audio/wav", read from the file rather than
    its name. `codec` is the first audio stream's codec as ffprobe names it, such as "mp3", "aac" or
    "pcm_s16le", and `profile` its profile where it has one, such as "LC" for AAC-LC. `seconds` is the
    audio stream's duration, falling back to the file's; `container_seconds` keeps the file's, which a
    longer video track can stretch. Each part is None when ffprobe didn't report it.
    """

    mime_type: str | None
    codec: str | None
    profile: str | None
    seconds: float | None
    container_seconds: float | None = None

    @property
    def shortest_seconds(self) -> float | None:
        """The shorter of the stream and container durations; a maximum is checked against this one."""
        return min(self._durations(), default=None)

    @property
    def longest_seconds(self) -> float | None:
        """The longer of the stream and container durations; a minimum is checked against this one."""
        return max(self._durations(), default=None)

    def _durations(self) -> list[float]:
        return [s for s in (self.seconds, self.container_seconds) if s is not None]


def local_audio_probe(value: Any) -> AudioProbe | None:
    """The container, codec and duration of an audio input, or None if it can't be read locally.

    Reads the first audio stream, so an audio track inside a video file is read too. Probes only a
    file on this machine, with an ffprobe that is already installed, under the same time limits as
    `local_video_probe`; see `_run_ffprobe`.
    """
    location = coerce_media_url_or_data_uri(value, kind="audio")
    if not location:
        return None
    return _read_once(MediaNeed.AUDIO, location, _probe_audio)


def _probe_audio(location: str) -> AudioProbe | None:
    """`local_audio_probe` for a location, without the per-pass cache."""
    data = _run_ffprobe(location, ["-show_format", "-select_streams", "a:0", "-show_streams"], kind="audio")
    if data is None:
        return None
    stream = _first_stream(data)
    if not stream:
        return None
    stream_seconds = _stream_seconds(stream)
    container_seconds = _seconds(data.get("format", {}).get("duration"))
    seconds = stream_seconds
    if seconds is None:
        seconds = container_seconds
    return AudioProbe(
        mime_type=_audio_mime_type(data),
        codec=_stream_text(stream, "codec_name"),
        profile=_stream_text(stream, "profile"),
        seconds=seconds,
        container_seconds=container_seconds,
    )


def _image_source(location: str) -> BytesIO | Path | None:
    """What Pillow should open for an image location: a data URI's bytes or a file on this machine."""
    if not location.startswith("data:"):
        return _local_path(location)
    payload = location.split(",", 1)[-1]
    try:
        return BytesIO(base64.b64decode(payload, validate=False))
    except (binascii.Error, ValueError) as e:
        logger.debug("Could not read image header for a pre-run input check, skipping it: %s", e)
        return None


def _run_ffprobe(value: Any, args: list[str], *, kind: MediaKind) -> dict[str, Any] | None:  # noqa: PLR0911
    """The JSON ffprobe prints for a video or audio input on this machine, or None.

    Only a file on this machine is probed: a remote URL would make ffprobe download it, and a data
    URI is too large to pass on its command line. ffprobe is used only if it is already installed
    (see `_installed_ffprobe`), never fetched, and is given `FFPROBE_TIMEOUT_SECONDS` to answer. A
    probe that runs out of time is logged as a warning.
    """
    location = coerce_media_url_or_data_uri(value, kind=kind)
    if not location or location.startswith("data:"):
        return None
    path = _local_path(location)
    if path is None:
        return None
    ffprobe = _installed_ffprobe()
    if ffprobe is None:
        logger.debug("ffprobe is not installed, skipping a pre-run %s check", kind)
        return None
    cmd = [ffprobe, "-v", "error", "-print_format", "json", *args, str(path)]
    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            # ffprobe writes UTF-8 whatever the locale; the default would decode it with Windows' code
            # page and fail on a non-Latin file name, which it echoes back.
            encoding="utf-8",
            errors="replace",
            check=True,
            timeout=FFPROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "Skipped checking %s before the run because ffprobe took longer than %g seconds: %s",
            _INPUT_NOUN[kind],
            FFPROBE_TIMEOUT_SECONDS,
            location,
        )
        return None
    except (subprocess.SubprocessError, OSError, ValueError) as e:
        logger.debug("Could not probe a %s for a pre-run input check, skipping it: %s", kind, e)
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError as e:
        logger.debug("Could not probe a %s for a pre-run input check, skipping it: %s", kind, e)
        return None
    if not isinstance(data, dict):
        return None
    _record_matroska_doctype(data, path)
    return data


# How the warning for a probe that ran out of time names the input, by its kind.
_INPUT_NOUN: dict[MediaKind, str] = {"image": "an image", "video": "a video", "audio": "an audio clip"}


def _record_matroska_doctype(data: dict[str, Any], path: Path) -> None:
    """Add a Matroska file's DocType ("webm" or "matroska") to ffprobe's JSON, which doesn't report it.

    ffprobe names MKV and WebM with one demuxer. The file's header says which it is, and that's more
    reliable than guessing from a codec, as WebM and MKV can carry the same ones.
    """
    format_info = data.get("format")
    if not isinstance(format_info, dict) or not str(format_info.get("format_name", "")).startswith("matroska"):
        return
    try:
        with path.open("rb") as file:
            header = file.read(_MATROSKA_HEADER_BYTES)
    except OSError:
        return
    doctype = _matroska_doctype(header)
    if doctype is not None:
        format_info[_DOCTYPE_KEY] = doctype


def _matroska_doctype(header: bytes) -> str | None:
    """The DocType in a Matroska file's EBML header, or None if it isn't found there."""
    start = header.find(_EBML_DOCTYPE_ID)
    if start < 0 or start + len(_EBML_DOCTYPE_ID) >= len(header):
        return None
    size_byte = header[start + len(_EBML_DOCTYPE_ID)]
    # A one-byte EBML size has its top bit set; the DocType is short enough to always use one.
    if not size_byte & 0x80:
        return None
    end = start + len(_EBML_DOCTYPE_ID) + 1 + (size_byte & 0x7F)
    if end > len(header):
        return None
    return header[start + len(_EBML_DOCTYPE_ID) + 1 : end].decode("ascii", errors="replace")


def _installed_ffprobe() -> str | None:
    """The path to an ffprobe that is already installed, or None.

    Prefers the static-ffmpeg copy the engine uses elsewhere, but only once its download has
    finished (it writes `installed.crumb` last); asking static-ffmpeg for the path would download it
    otherwise. Falls back to an ffprobe on PATH.

    `get_platform_dir()` reads static-ffmpeg's `SELF_DIR` each time it is called, and the engine
    redirects `SELF_DIR` at boot (`utils/ffmpeg_cache.py`), so this looks in the directory the
    engine downloads ffmpeg to.
    """
    try:
        platform_dir = Path(static_ffmpeg_run.get_platform_dir())
    except OSError:
        return shutil.which("ffprobe")
    if not (platform_dir / "installed.crumb").is_file():
        return shutil.which("ffprobe")
    if sys.platform == "win32":
        exe = platform_dir / "ffprobe.exe"
    else:
        exe = platform_dir / "ffprobe"
    if not exe.is_file():
        return shutil.which("ffprobe")
    return str(exe)


def _local_path(location: str) -> Path | None:
    """The file on this machine a location names, or None for a remote URL or an unresolvable path."""
    try:
        resolved = File(location).resolve()
    except (FileLoadError, OSError, ValueError):
        return None
    if is_url(resolved):
        return None
    path = Path(resolved)
    # A name longer than the OS allows raises rather than reading as missing.
    try:
        is_file = path.is_file()
    except (OSError, ValueError):
        return None
    if not is_file:
        return None
    return path


def _first_stream(data: dict[str, Any]) -> dict[str, Any]:
    """The first stream ffprobe selected, or an empty dict when there is none."""
    streams = data.get("streams")
    if not streams:
        return {}
    return streams[0]


def _stream_text(stream: dict[str, Any], key: str) -> str | None:
    """A text field of an ffprobe stream, or None when it's missing or ffprobe's "unknown"."""
    value = stream.get(key)
    if not isinstance(value, str) or not value or value == "unknown":
        return None
    return value


def _audio_mime_type(data: dict[str, Any]) -> str | None:
    """An audio input's container as a MIME type, read from ffprobe's JSON, or None if it isn't named here.

    ffprobe names MP4 and M4A with one demuxer, so an MP4 reads as audio/mp4 here, the audio in it
    being what an audio check looks at.
    """
    format_info = data.get("format")
    if not isinstance(format_info, dict):
        return None
    demuxer = format_info.get("format_name")
    if not isinstance(demuxer, str):
        return None
    if demuxer.startswith("mov,"):
        return _iso_audio_mime_type(format_info.get("tags", {}).get("major_brand"))
    if demuxer.startswith("matroska"):
        return _matroska_audio_mime_type(format_info, _stream_text(_first_stream(data), "codec_name"))
    return _AUDIO_DEMUXER_MIME_TYPES.get(demuxer) or _unmapped_mime_type("audio", demuxer)


def _iso_audio_mime_type(brand: Any) -> str:
    """An ISO media file's container for an audio check.

    A MOV stays a MOV, a 3GP reads as 3GP audio, and the rest reads as M4A.
    """
    mime_type = _iso_media_mime_type(brand)
    match mime_type:
        case "video/quicktime" | "image/heif" | "image/avif":
            return mime_type
        case "video/3gpp":
            return "audio/3gpp"
        case "video/3gpp2":
            return "audio/3gpp2"
        case _:
            return "audio/mp4"


def _matroska_audio_mime_type(format_info: dict[str, Any], codec: str | None) -> str:
    """A Matroska file's container for an audio check: WebM or MKA, by the file's DocType."""
    if _is_webm(format_info, codec, _WEBM_AUDIO_CODECS):
        return "audio/webm"
    return "audio/x-matroska"


def _video_codec(data: dict[str, Any]) -> str | None:
    """The first video stream's codec in ffprobe's JSON, such as "h264", or None."""
    codec = _first_stream(data).get("codec_name")
    if isinstance(codec, str) and codec:
        return codec
    return None


def _video_mime_type(data: dict[str, Any]) -> str | None:
    """A video's container as a MIME type, read from ffprobe's JSON, or None if it's one not named here.

    ffprobe names MP4, MOV and 3GP with one demuxer ("mov,mp4,m4a,3gp,3g2,mj2"), and MKV and WebM with
    another, so the file's brand tells MP4 from MOV, and its DocType WebM from MKV.
    """
    format_info = data.get("format")
    if not isinstance(format_info, dict):
        return None
    demuxer = format_info.get("format_name")
    if not isinstance(demuxer, str):
        return None
    if demuxer.startswith("mov,"):
        brand = format_info.get("tags", {}).get("major_brand")
        return _iso_media_mime_type(brand)
    if demuxer.startswith("matroska"):
        if _is_webm(format_info, _video_codec(data), _WEBM_VIDEO_CODECS):
            return "video/webm"
        return "video/x-matroska"
    return _DEMUXER_MIME_TYPES.get(demuxer) or _unmapped_mime_type("video", demuxer)


def _is_webm(format_info: dict[str, Any], codec: str | None, webm_codecs: frozenset[str]) -> bool:
    """Whether a Matroska file is WebM: by the DocType in its header, or by its codec when that's unread."""
    doctype = format_info.get(_DOCTYPE_KEY)
    if doctype is not None:
        return doctype == "webm"
    return codec in webm_codecs


def _unmapped_mime_type(kind: str, demuxer: str) -> str:
    """A MIME type for a container ffprobe named but this module doesn't map, after its demuxer.

    The probe read the file and named its format, so a format check refuses it rather than skipping
    a format it has read, as `local_image_info` does for an image format Pillow has no type for.
    """
    name = demuxer.split(",", 1)[0]
    # ffmpeg reads a still image with a "<format>_pipe" demuxer; name it as the image it is.
    if name.endswith("_pipe"):
        return f"image/{name.removesuffix('_pipe')}"
    # A "-ffprobe-" name can't collide with an alias such as audio/x-mpeg, which means MP3.
    return f"{kind}/x-ffprobe-{name}"


def _iso_media_mime_type(brand: Any) -> str:
    """The MIME type an ISO media file's major brand names.

    A file with no brand is old QuickTime, so it reads as MOV, like one branded "qt". HEIC and AVIF
    photos are ISO media files too; they read as images, so a video check refuses them.
    """
    if not isinstance(brand, str):
        return "video/quicktime"
    brand = brand.strip().lower()
    known = _ISO_BRAND_MIME_TYPES.get(brand)
    if known is not None:
        return known
    if brand.startswith("3gp"):
        return "video/3gpp"
    if brand.startswith("3g2"):
        return "video/3gpp2"
    return "video/mp4"


def _frame_size(data: dict[str, Any]) -> ImageInfo | None:
    """The frame size, turned upright for a rotated clip, in ffprobe's JSON for the first video stream, or None."""
    streams = data.get("streams")
    if not streams:
        return None
    stream = streams[0]
    try:
        width, height = int(stream["width"]), int(stream["height"])
    except (KeyError, TypeError, ValueError):
        return None
    # ffprobe reports 0x0 when it can't work out the frame size; that's unread, not tiny.
    if width <= 0 or height <= 0:
        return None
    if _is_quarter_turn(stream):
        return ImageInfo(width=height, height=width, mime_type=None)
    return ImageInfo(width=width, height=height, mime_type=None)


def _display_frame_size(data: dict[str, Any]) -> ImageInfo | None:
    """The frame's shape as it plays, when its pixels aren't square, or None when they are.

    The width is scaled by the stream's sample aspect ratio, so 1440x1080 at 4:3 reads as 1920x1080.
    Only shape checks use it; pixel-count limits apply to the stored frame.
    """
    stored = _frame_size(data)
    if stored is None:
        return None
    sar = _parse_fraction(_first_stream(data).get("sample_aspect_ratio"))
    if sar is None or sar == 1:
        return None
    stream = _first_stream(data)
    if _is_quarter_turn(stream):
        return ImageInfo(width=stored.width, height=round(stored.height * sar), mime_type=None)
    return ImageInfo(width=round(stored.width * sar), height=stored.height, mime_type=None)


def _parse_fraction(value: Any) -> float | None:
    """An ffprobe "N:D" ratio as a positive number, or None when it's missing, "0:1" or "N/A"."""
    if not isinstance(value, str):
        return None
    numerator, _, denominator = value.partition(":")
    try:
        ratio = float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        return None
    if not math.isfinite(ratio) or ratio <= 0:
        return None
    return ratio


def _is_quarter_turn(stream: dict[str, Any]) -> bool:
    """Whether a video stream is displayed rotated by 90 degrees, so its width and height swap.

    The display matrix in `side_data_list` wins over the older `rotate` tag.
    """
    rotation = stream.get("tags", {}).get("rotate")
    for side_data in stream.get("side_data_list", []):
        rotation = side_data.get("rotation", rotation)
    if rotation is None:
        return False
    try:
        degrees = abs(int(float(rotation)))
    except (TypeError, ValueError):
        return False
    return degrees % _HALF_TURN_DEGREES == _QUARTER_TURN_DEGREES


def _nominal_frame_rate(data: dict[str, Any]) -> float | None:
    """The first video stream's declared frame rate (`r_frame_rate`) in ffprobe's JSON, or None."""
    streams = data.get("streams")
    if not streams:
        return None
    return _positive_fraction(streams[0].get("r_frame_rate", ""))


def _frame_rate(data: dict[str, Any]) -> float | None:
    """The first video stream's playback frame rate in ffprobe's JSON, or None.

    FFmpeg's avformat.h documents `r_frame_rate` as the lowest rate at which all timestamps can be
    represented exactly and notes it is "just a guess". `avg_frame_rate` is the actual average
    playback rate and is preferred. Falls back to `r_frame_rate` when the average is unset ("0/0"),
    then to `nb_frames` / `duration`.
    """
    streams = data.get("streams")
    if not streams:
        return None
    stream = streams[0]

    average = _positive_fraction(stream.get("avg_frame_rate", ""))
    if average is not None:
        return average

    declared = _positive_fraction(stream.get("r_frame_rate", ""))
    if declared is not None:
        return declared

    frames = _whole_number(stream.get("nb_frames"))
    duration = _seconds(stream.get("duration"))
    if frames is None or frames <= 0 or duration is None or duration <= 0:
        return None
    return frames / duration


def _positive_fraction(text: str) -> float | None:
    """An ffprobe rate such as "30/1" or "30000/1001" as a number, or None when it isn't above 0.

    ffprobe writes "0/0" for a rate it doesn't know.
    """
    if not text or text == "0/0":
        return None
    parts = text.split("/")
    if len(parts) != 2:  # noqa: PLR2004
        return None
    try:
        numerator = float(parts[0])
        denominator = float(parts[1])
    except ValueError:
        return None
    if denominator == 0:
        return None
    rate = numerator / denominator
    if not rate > 0:
        return None
    return rate


def _recorded_frames(data: dict[str, Any]) -> int | None:
    """The frame count the first video stream records in ffprobe's JSON, or None when it records none."""
    frames = _whole_number(_first_stream(data).get("nb_frames"))
    if frames is None or frames <= 0:
        return None
    return frames


def _whole_number(value: Any) -> int | None:
    """An ffprobe count field such as `nb_frames` as an int, or None when it's missing or not one."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _stream_seconds(stream: dict[str, Any]) -> float | None:
    """A stream's duration in ffprobe's JSON, or None.

    Matroska and WebM give a stream's length only as a "HH:MM:SS.nnnnnnnnn" DURATION tag, with
    `duration` missing, so the tag is read when the field isn't there.
    """
    seconds = _seconds(stream.get("duration"))
    if seconds is not None:
        return seconds
    tags = stream.get("tags")
    if not isinstance(tags, dict):
        return None
    seconds = _clock_seconds(tags.get("DURATION"))
    if seconds is not None:
        return seconds
    # A tag can carry a language suffix, such as DURATION-eng, when no plain one was written.
    for key, value in tags.items():
        if isinstance(key, str) and key.upper().startswith("DURATION-"):
            return _clock_seconds(value)
    return None


def _clock_seconds(value: Any) -> float | None:
    """A "HH:MM:SS.fraction" duration as seconds, or None when it isn't one."""
    # A sign would be lost in the hours ("-00" reads as 0), so a signed time isn't a duration.
    if not isinstance(value, str) or value.strip().startswith(("-", "+")):
        return None
    hours, _, rest = value.partition(":")
    minutes, _, secs = rest.partition(":")
    try:
        seconds = int(hours) * 3600 + int(minutes) * 60 + float(secs)
    except ValueError:
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def _seconds(value: Any) -> float | None:
    """An ffprobe duration field as seconds, or None when it's missing or not a number."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# A Matroska file's EBML header holds its DocType within its first few dozen bytes.
_EBML_DOCTYPE_ID = b"\x42\x82"
_MATROSKA_HEADER_BYTES = 64
# Where _run_ffprobe records a Matroska file's DocType in ffprobe's format JSON.
_DOCTYPE_KEY = "griptape_doctype"

# Video and audio codecs a WebM file may carry, used when a Matroska file's DocType can't be read.
_WEBM_VIDEO_CODECS = frozenset({"vp8", "vp9", "av1"})
_WEBM_AUDIO_CODECS = frozenset({"opus", "vorbis"})

# ISO media major brands that name something other than MP4 video: QuickTime, and HEIC/HEIF and AVIF
# still images.
_ISO_BRAND_MIME_TYPES = {
    "": "video/quicktime",
    "qt": "video/quicktime",
    **dict.fromkeys(("heic", "heix", "heim", "heis", "hevc", "hevx", "hevm", "hevs", "mif1", "msf1"), "image/heif"),
    **dict.fromkeys(("avif", "avis"), "image/avif"),
}


# ffprobe demuxer names for audio containers that name one format each.
_AUDIO_DEMUXER_MIME_TYPES = {
    "asf": "audio/x-ms-wma",
    "caf": "audio/x-caf",
    "au": "audio/basic",
    "ac3": "audio/ac3",
    "avi": "video/x-msvideo",
    "mpegts": "video/mp2t",
    "mpeg": "video/mpeg",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
    "aac": "audio/aac",
    "aiff": "audio/aiff",
    "w64": "audio/wav",
}

# ffprobe demuxer names for containers that name one format each.
_DEMUXER_MIME_TYPES = {
    "ogg": "video/ogg",
    "mxf": "application/mxf",
    "dv": "video/dv",
    "avi": "video/x-msvideo",
    "flv": "video/x-flv",
    "mpegts": "video/mp2t",
    "mpeg": "video/mpeg",
    "asf": "video/x-ms-wmv",
    "gif": "image/gif",
}
