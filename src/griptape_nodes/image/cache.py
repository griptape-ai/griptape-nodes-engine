"""Process-wide image cache freshness and source coordination."""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Condition, RLock, local
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

import OpenImageIO as oiio  # noqa: N813

from griptape_nodes.files.path_utils import canonicalize_for_identity

_WINDOWS_LONG_PATH_PREFIX = "\\\\?\\"


_MAX_FINGERPRINTS = 4096
_registry_lock = RLock()
_source_locks: WeakValueDictionary[str, RLock] = WeakValueDictionary()
_fingerprints: OrderedDict[str, SourceFingerprint] = OrderedDict()


_gate = Condition(RLock())
_thread_access = local()
_active_sources = 0
_directory_mutating = False
_directory_waiters = 0


@dataclass(frozen=True)
class SourceFingerprint:
    """Filesystem identity used to detect source replacement and same-second writes."""

    size: int
    modified_ns: int
    device: int
    inode: int


@contextmanager
def image_source_access(path: str | Path) -> Iterator[None]:
    """Coordinate cache consumption and engine mutations for one canonical source."""
    key = str(canonicalize_for_identity(path))
    with _registry_lock:
        lock = _source_locks.get(key)
        if lock is None:
            lock = RLock()
            _source_locks[key] = lock
    with _source_gate(), lock:
        yield


@contextmanager
def _source_gate() -> Iterator[None]:
    global _active_sources  # noqa: PLW0603
    depth = getattr(_thread_access, "depth", 0)
    if depth == 0:
        with _gate:
            _gate.wait_for(lambda: not _directory_mutating and _directory_waiters == 0)
            _active_sources += 1
    _thread_access.depth = depth + 1
    try:
        yield
    finally:
        _thread_access.depth -= 1
        if depth == 0:
            with _gate:
                _active_sources -= 1
                _gate.notify_all()


@contextmanager
def image_path_mutation(path: str | Path, *, is_directory: bool) -> Iterator[None]:
    """Invalidate a file around a mutation, or exclude all readers for a directory."""
    if is_directory:
        with image_directory_mutation():
            yield
        return
    with image_source_access(path):
        invalidate_image_cache(path)
        try:
            yield
        finally:
            invalidate_image_cache(path)


@contextmanager
def image_directory_mutation() -> Iterator[None]:
    """Exclude source consumers while mutating a directory and its descendants."""
    _begin_directory_mutation()
    try:
        invalidate_all_image_cache()
        yield
    finally:
        try:
            invalidate_all_image_cache()
        finally:
            _end_directory_mutation()


def _begin_directory_mutation() -> None:
    global _directory_mutating, _directory_waiters  # noqa: PLW0603
    with _gate:
        _directory_waiters += 1
        try:
            _gate.wait_for(lambda: not _directory_mutating and _active_sources == 0)
            _directory_mutating = True
        finally:
            _directory_waiters -= 1


def _end_directory_mutation() -> None:
    global _directory_mutating  # noqa: PLW0603
    with _gate:
        _directory_mutating = False
        _gate.notify_all()


def refresh_image_cache(path: str | Path) -> None:
    """Evict a changed source before opening its cache-backed pixels."""
    canonical = canonicalize_for_identity(path)
    key = str(canonical)
    with image_source_access(path):
        try:
            stat = canonical.stat()
        except OSError:
            invalidate_image_cache(path)
            raise
        fingerprint = SourceFingerprint(stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino)
        with _registry_lock:
            previous = _fingerprints.get(key)
        if previous != fingerprint:
            invalidate_image_cache(path)
        with _registry_lock:
            _fingerprints[key] = fingerprint
            _fingerprints.move_to_end(key)
            while len(_fingerprints) > _MAX_FINGERPRINTS:
                _fingerprints.popitem(last=False)


def invalidate_all_image_cache() -> None:
    """Release cached descendant handles before directory operations."""
    with _registry_lock:
        oiio.ImageCache(shared=True).invalidate_all(True)
        _fingerprints.clear()


def invalidate_image_cache(path: str | Path) -> None:
    """Evict cached tiles and close any open ImageInput handle for ``path`` in the shared ImageCache.

    Args:
        path: Local file path to invalidate.
    """
    cache = oiio.ImageCache(shared=True)
    raw = str(path)
    candidates = {raw}
    if raw.startswith(_WINDOWS_LONG_PATH_PREFIX):
        candidates.add(raw.removeprefix(_WINDOWS_LONG_PATH_PREFIX))
    candidates.add(str(canonicalize_for_identity(path)))

    with image_source_access(path):
        for candidate in candidates:
            cache.invalidate(candidate, True)
        with _registry_lock:
            _fingerprints.pop(str(canonicalize_for_identity(path)), None)
