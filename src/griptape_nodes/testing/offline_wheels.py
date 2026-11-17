"""Hand-built wheels for dependency-environment tests.

Building the wheel by hand keeps these tests fully offline: no network, no build backend, and
no dependency on any package that happens to be installed. Because the packages built here
exist nowhere else, a successful import of one is proof that the environment it was installed
into was created AND wired onto ``sys.path``.
"""

from __future__ import annotations

import base64
import hashlib
import zipfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def build_wheel(wheel_dir: Path, name: str, version: str = "1.0.0") -> Path:
    """Write a minimal importable wheel for ``name`` into ``wheel_dir``.

    The built package exposes ``__version__`` so tests can assert *which* environment
    satisfied an import.
    """
    wheel_dir.mkdir(parents=True, exist_ok=True)
    wheel_path = wheel_dir / f"{name}-{version}-py3-none-any.whl"
    dist_info = f"{name}-{version}.dist-info"
    files = {
        f"{name}/__init__.py": f'__version__ = "{version}"\n',
        f"{dist_info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        f"{dist_info}/WHEEL": "Wheel-Version: 1.0\nGenerator: test-fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record_rows = []
    with zipfile.ZipFile(wheel_path, "w") as zf:
        for file_name, content in files.items():
            data = content.encode()
            zf.writestr(file_name, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            record_rows.append(f"{file_name},sha256={digest},{len(data)}")
        record_rows.append(f"{dist_info}/RECORD,,")
        zf.writestr(f"{dist_info}/RECORD", "\n".join(record_rows) + "\n")
    return wheel_path


def offline_install_flags(wheel_dir: Path) -> list[str]:
    """Pip flags that install only from ``wheel_dir``, never the network."""
    return ["--no-index", "--find-links", str(wheel_dir)]
