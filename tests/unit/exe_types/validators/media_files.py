"""Real image, video and audio files for the validator tests, made with Pillow and ffmpeg.

`fixtures/photo_64x48.heic` is a real 64x48 HEIC photo (HEVC-coded, brand "heic"), 490 bytes, made
with macOS's `sips -s format heic` from a solid-colour PNG, since Pillow can't write HEIC without a
plugin the engine doesn't install.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
from io import BytesIO
from pathlib import Path

from PIL import Image

HEIC_PHOTO = Path(__file__).parent / "fixtures" / "photo_64x48.heic"

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
HAS_FFMPEG = FFMPEG is not None and FFPROBE is not None


def png_bytes(width: int, height: int, image_format: str = "PNG") -> bytes:
    """A blank image of this size, saved in `image_format`."""
    buffer = BytesIO()
    Image.new("RGB", (width, height)).save(buffer, format=image_format)
    return buffer.getvalue()


def data_uri(data: bytes, mime_type: str = "image/png") -> str:
    """`data` as a base64 data URI."""
    return f"data:{mime_type};base64,{base64.b64encode(data).decode()}"


def image_file(directory: Path, width: int, height: int, image_format: str = "PNG") -> Path:
    """A blank image of this size, written to a file in `directory`."""
    path = directory / f"image_{width}x{height}.{image_format.lower()}"
    path.write_bytes(png_bytes(width, height, image_format))
    return path


def video_file(  # noqa: PLR0913
    directory: Path,
    *,
    width: int = 320,
    height: int = 240,
    fps: int = 24,
    seconds: float = 2,
    codec: str = "libx264",
    name: str = "clip.mp4",
) -> Path:
    """A real clip with a test pattern, encoded by ffmpeg."""
    if FFMPEG is None:
        msg = "ffmpeg is needed to make a test clip"
        raise RuntimeError(msg)
    path = directory / name
    source = f"testsrc=size={width}x{height}:rate={fps}:duration={seconds}"
    cmd = [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", source, "-c:v", codec, "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True, timeout=60)  # noqa: S603
    return path


def audio_file(directory: Path, *, seconds: float = 3, codec: str = "libmp3lame", name: str = "tone.mp3") -> Path:
    """A real tone, encoded by ffmpeg."""
    if FFMPEG is None:
        msg = "ffmpeg is needed to make a test clip"
        raise RuntimeError(msg)
    path = directory / name
    source = f"sine=frequency=440:duration={seconds}"
    cmd = [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", source, "-c:a", codec, str(path)]
    subprocess.run(cmd, check=True, timeout=60)  # noqa: S603
    return path
