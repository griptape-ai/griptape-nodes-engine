"""Time and measure peak memory of each GriptapeImage backend on the engine's image operations.

Each (backend, operation) runs in its own subprocess. Peak memory is the process's peak RSS
during the operation minus its RSS just before, so it counts native allocations (OpenCV and
OpenImageIO pixel buffers) that tracemalloc can't see. Linux only, since it resets the peak
through /proc/self/clear_refs.

    uv run python scripts/benchmark_image_backends.py [--repeat N] [--markdown]
"""

# A command-line report that runs itself as subprocesses
# ruff: noqa: T201, S603

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from io import BytesIO
from pathlib import Path

BACKENDS = ("pillow", "opencv", "openimageio")
WORKFLOW_TEXT = '{"nodes": [' + ", ".join(['{"name": "node", "value": 1}'] * 4000) + "]}"


def _make_inputs(directory: Path) -> dict[str, Path]:
    """Write the benchmark inputs: photo-sized 8-bit images and a 16-bit PNG."""
    import cv2
    import numpy as np
    from PIL import Image, PngImagePlugin

    rng = np.random.default_rng(0)
    height, width = 3000, 4000
    gradient = np.linspace(0, 255, width, dtype=np.float32)[None, :, None]
    noise = rng.normal(0, 24, (height, width, 3)).astype(np.float32)
    photo = np.clip(gradient + noise, 0, 255).astype(np.uint8)

    inputs = {}
    source = Image.fromarray(photo)
    exif = source.getexif()
    exif[0x0112] = 6  # Orientation: rotate 90 clockwise
    exif[0x010F] = "Camera Maker"
    inputs["jpeg_4000x3000"] = directory / "photo.jpg"
    source.save(inputs["jpeg_4000x3000"], quality=90, exif=exif)

    inputs["png_rgba_4000x3000"] = directory / "photo.png"
    rgba = np.dstack([photo, np.full((height, width), 255, np.uint8)])
    Image.fromarray(rgba).save(inputs["png_rgba_4000x3000"])

    # A PNG that already carries an embedded workflow, as a saved Griptape Nodes output does
    info = PngImagePlugin.PngInfo()
    info.add_text("gtn_workflow", WORKFLOW_TEXT, zip=True)
    inputs["png_rgba_4000x3000_workflow"] = directory / "photo_workflow.png"
    Image.fromarray(rgba).save(inputs["png_rgba_4000x3000_workflow"], pnginfo=info)

    inputs["png16_rgb_2048x2048"] = directory / "deep.png"
    deep = (rng.integers(0, 65536, (2048, 2048, 3))).astype(np.uint16)
    cv2.imwrite(str(inputs["png16_rgb_2048x2048"]), deep)
    return inputs


def _operation(name: str, data: bytes) -> None:
    from griptape_nodes.image import open_image

    if name == "read_text_metadata":
        with open_image(data) as image:
            _ = image.text_metadata
    elif name == "inject_png_metadata":
        # PngMetadataDriver.inject_metadata
        with open_image(data) as image:
            texts = {**image.text_metadata, "gtn_workflow": WORKFLOW_TEXT}
            image.encode("PNG", text_metadata=texts)
    elif name == "inject_exif_metadata":
        # ExifMetadataDriver.inject_metadata
        with open_image(data) as image:
            image.encode(image.format or "JPEG", exif_bytes=image.exif_bytes)
    elif name == "webp_preview":
        # utils.image_preview: orient, to RGB, fit 512x512, WEBP q85
        with open_image(data) as image, image.exif_transposed() as upright, upright.to_rgb() as rgb:
            rgb.thumbnail(512, 512).encode("WEBP", quality=85, optimize=True)
    elif name == "rounded_jpeg_preview":
        # PILRoundedPreviewGenerator with JPEG output
        with (
            open_image(data) as image,
            image.exif_transposed() as upright,
            upright.thumbnail(1024, 1024) as thumbnail,
            thumbnail.with_rounded_corners(40) as rounded,
            rounded.flattened((255, 255, 255)) as flat,
        ):
            flat.encode("JPEG")
    elif name == "full_png_reencode":
        with open_image(data) as image:
            image.encode("PNG")
    else:
        msg = f"Unknown operation {name}"
        raise ValueError(msg)


# Operation -> input it runs on
OPERATIONS = {
    "read_text_metadata": "png_rgba_4000x3000_workflow",
    "inject_png_metadata": "png_rgba_4000x3000_workflow",
    "inject_exif_metadata": "jpeg_4000x3000",
    "webp_preview": "jpeg_4000x3000",
    "rounded_jpeg_preview": "jpeg_4000x3000",
    "full_png_reencode": "png16_rgb_2048x2048",
}


def _status_mib(field: str) -> float:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith(f"{field}:"):
            return int(line.split()[1]) / 1024
    msg = f"{field} missing from /proc/self/status"
    raise RuntimeError(msg)


def _warm_up(data: bytes) -> None:
    """Load the backend's codec for this format on a tiny image, leaving no large freed memory to reuse."""
    from PIL import Image

    from griptape_nodes.image import open_image

    image_format = Image.open(BytesIO(data)).format or "PNG"
    buffer = BytesIO()
    Image.new("RGB", (16, 16)).save(buffer, format=image_format)
    with open_image(buffer.getvalue()) as image:
        image.thumbnail(8, 8).encode(image_format)


def _child(backend: str, operation: str, input_path: Path, repeat: int) -> None:
    os.environ["GTN_IMAGE_BACKEND"] = backend
    data = input_path.read_bytes()
    _warm_up(data)

    # Peak is measured on the first full-size run only: later runs reuse memory the allocator
    # kept from earlier ones, which hides how much an operation needs
    Path("/proc/self/clear_refs").write_text("5")  # resets VmHWM to the current RSS
    before = _status_mib("VmRSS")
    _operation(operation, data)
    peak = _status_mib("VmHWM") - before

    timings = []
    for _ in range(repeat):
        start = time.perf_counter()
        _operation(operation, data)
        timings.append(time.perf_counter() - start)
    print(json.dumps({"median_ms": statistics.median(timings) * 1000, "peak_mib": peak}))


# Backend -> the library it wraps, imported alone (griptape_nodes.image imports all three)
LIBRARY_MODULES = {"pillow": "PIL.Image", "opencv": "cv2", "openimageio": "OpenImageIO"}


def _import_cost(backend: str) -> dict[str, float]:
    code = (
        "import time; start = time.perf_counter();"
        f"import {LIBRARY_MODULES[backend]};"
        "elapsed = time.perf_counter() - start;"
        "rss = next(int(l.split()[1]) for l in open('/proc/self/status') if l.startswith('VmRSS:'));"
        "print(elapsed * 1000, rss / 1024)"
    )
    output = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.split()
    return {"import_ms": float(output[0]), "rss_after_import_mib": float(output[1])}


def main() -> None:
    """Run every operation under every backend and print the results."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--markdown", action="store_true")
    parser.add_argument("--child", nargs=3, metavar=("BACKEND", "OPERATION", "INPUT"))
    args = parser.parse_args()

    if args.child:
        backend, operation, input_path = args.child
        _child(backend, operation, Path(input_path), args.repeat)
        return

    results: dict[str, dict[str, dict[str, float]]] = {}
    with tempfile.TemporaryDirectory() as directory:
        inputs = _make_inputs(Path(directory))
        for operation, input_name in OPERATIONS.items():
            results[operation] = {}
            for backend in BACKENDS:
                command = [
                    sys.executable,
                    __file__,
                    "--repeat",
                    str(args.repeat),
                    "--child",
                    backend,
                    operation,
                    str(inputs[input_name]),
                ]
                completed = subprocess.run(command, capture_output=True, text=True, check=False)
                if completed.returncode != 0:
                    error = completed.stderr.strip().splitlines() or [f"exit code {completed.returncode}"]
                    results[operation][backend] = {"error": error[-1]}  # type: ignore[dict-item]
                else:
                    results[operation][backend] = json.loads(completed.stdout.strip().splitlines()[-1])
    imports = {backend: _import_cost(backend) for backend in BACKENDS}

    if not args.markdown:
        print(json.dumps({"operations": results, "imports": imports}, indent=2))
        return

    header = "| Operation (input) | " + " | ".join(BACKENDS) + " |"
    print("Median wall clock (ms), lower is better\n")
    print(header)
    print("| --- |" + " ---: |" * len(BACKENDS))
    for operation, by_backend in results.items():
        cells = [_cell(by_backend[backend], "median_ms") for backend in BACKENDS]
        print(f"| `{operation}` ({OPERATIONS[operation]}) | " + " | ".join(cells) + " |")
    print("\nPeak RSS above the pre-operation RSS (MiB), lower is better\n")
    print(header)
    print("| --- |" + " ---: |" * len(BACKENDS))
    for operation, by_backend in results.items():
        cells = [_cell(by_backend[backend], "peak_mib") for backend in BACKENDS]
        print(f"| `{operation}` ({OPERATIONS[operation]}) | " + " | ".join(cells) + " |")
    print("\nImport cost\n")
    print("| | " + " | ".join(BACKENDS) + " |")
    print("| --- |" + " ---: |" * len(BACKENDS))
    print("| Import time (ms) | " + " | ".join(f"{imports[b]['import_ms']:.0f}" for b in BACKENDS) + " |")
    print(
        "| RSS after import (MiB) | " + " | ".join(f"{imports[b]['rss_after_import_mib']:.0f}" for b in BACKENDS) + " |"
    )


def _cell(result: dict[str, float], key: str) -> str:
    if "error" in result:
        return "error"
    return f"{result[key]:.1f}"


if __name__ == "__main__":
    main()
