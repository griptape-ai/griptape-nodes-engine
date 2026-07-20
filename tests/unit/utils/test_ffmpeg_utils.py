"""Tests for ffmpeg/ffprobe binary resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from griptape_nodes.utils import ffmpeg_utils
from griptape_nodes.utils.ffmpeg_utils import (
    FFmpegBinaries,
    _resolve_ffmpeg_binaries,
    resolve_ffmpeg_binaries,
)

if TYPE_CHECKING:
    from collections.abc import Generator

FFMPEG_KEY = "ffmpeg_path"
FFPROBE_KEY = "ffprobe_path"


@pytest.fixture(autouse=True)
def clear_resolver_cache() -> Generator[None, None, None]:
    """The resolver is cached on its arguments; clear it around every test."""
    _resolve_ffmpeg_binaries.cache_clear()
    yield
    _resolve_ffmpeg_binaries.cache_clear()


def _resolve(configured_ffmpeg: str | None = None, configured_ffprobe: str | None = None) -> FFmpegBinaries:
    """Call the cached resolver with the standard setting-key names."""
    return _resolve_ffmpeg_binaries(configured_ffmpeg, configured_ffprobe, FFMPEG_KEY, FFPROBE_KEY)


def _fake_which(mapping: dict[str, str]) -> object:
    """Build a shutil.which stand-in that returns paths from a name/path mapping."""

    def which(cmd: str) -> str | None:
        return mapping.get(cmd)

    return which


class TestResolveFromPath:
    """Resolution from the system PATH."""

    def test_prefers_path_binaries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When both binaries are on PATH, they are used and static-ffmpeg is not called."""
        monkeypatch.setattr(
            ffmpeg_utils.shutil,
            "which",
            _fake_which({"ffmpeg": "/usr/bin/ffmpeg", "ffprobe": "/usr/bin/ffprobe"}),
        )

        def fail_fetch() -> tuple[str, str]:
            pytest.fail("static-ffmpeg should not be called when PATH binaries exist")

        monkeypatch.setattr(ffmpeg_utils.static_ffmpeg_run, "get_or_fetch_platform_executables_else_raise", fail_fetch)

        result = _resolve()

        assert result == FFmpegBinaries(ffmpeg="/usr/bin/ffmpeg", ffprobe="/usr/bin/ffprobe")

    def test_missing_binary_falls_back_to_static(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ffmpeg on PATH but ffprobe missing pulls only ffprobe from static-ffmpeg."""
        monkeypatch.setattr(
            ffmpeg_utils.shutil,
            "which",
            _fake_which({"ffmpeg": "/usr/bin/ffmpeg"}),
        )
        monkeypatch.setattr(
            ffmpeg_utils.static_ffmpeg_run,
            "get_or_fetch_platform_executables_else_raise",
            lambda: ("/static/ffmpeg", "/static/ffprobe"),
        )

        result = _resolve()

        assert result == FFmpegBinaries(ffmpeg="/usr/bin/ffmpeg", ffprobe="/static/ffprobe")


class TestResolveFromStatic:
    """Fallback to the static-ffmpeg download."""

    def test_no_binaries_uses_static(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With nothing on PATH, both binaries come from static-ffmpeg."""
        monkeypatch.setattr(ffmpeg_utils.shutil, "which", _fake_which({}))
        monkeypatch.setattr(
            ffmpeg_utils.static_ffmpeg_run,
            "get_or_fetch_platform_executables_else_raise",
            lambda: ("/static/ffmpeg", "/static/ffprobe"),
        )

        result = _resolve()

        assert result == FFmpegBinaries(ffmpeg="/static/ffmpeg", ffprobe="/static/ffprobe")

    def test_static_download_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A static-ffmpeg download failure surfaces as FileNotFoundError."""
        monkeypatch.setattr(ffmpeg_utils.shutil, "which", _fake_which({}))

        def raise_fetch() -> tuple[str, str]:
            msg = "404 Client Error"
            raise RuntimeError(msg)

        monkeypatch.setattr(ffmpeg_utils.static_ffmpeg_run, "get_or_fetch_platform_executables_else_raise", raise_fetch)

        with pytest.raises(FileNotFoundError, match="static-ffmpeg"):
            _resolve()

    def test_download_failure_is_not_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A transient download failure can be retried; only success is cached."""
        monkeypatch.setattr(ffmpeg_utils.shutil, "which", _fake_which({}))

        calls = {"count": 0}

        def flaky_fetch() -> tuple[str, str]:
            calls["count"] += 1
            if calls["count"] == 1:
                msg = "transient network error"
                raise RuntimeError(msg)
            return ("/static/ffmpeg", "/static/ffprobe")

        monkeypatch.setattr(ffmpeg_utils.static_ffmpeg_run, "get_or_fetch_platform_executables_else_raise", flaky_fetch)

        with pytest.raises(FileNotFoundError):
            _resolve()

        result = _resolve()
        assert result == FFmpegBinaries(ffmpeg="/static/ffmpeg", ffprobe="/static/ffprobe")


class TestResolveFromConfig:
    """Resolution from explicitly configured paths."""

    def test_configured_paths_take_precedence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Configured paths win over PATH, and static-ffmpeg is not called."""
        monkeypatch.setattr(
            ffmpeg_utils.shutil,
            "which",
            _fake_which(
                {
                    "ffmpeg": "/usr/bin/ffmpeg",
                    "ffprobe": "/usr/bin/ffprobe",
                    "/opt/ffmpeg": "/opt/ffmpeg",
                    "/opt/ffprobe": "/opt/ffprobe",
                }
            ),
        )

        def fail_fetch() -> tuple[str, str]:
            pytest.fail("static-ffmpeg should not be called when configured paths resolve")

        monkeypatch.setattr(ffmpeg_utils.static_ffmpeg_run, "get_or_fetch_platform_executables_else_raise", fail_fetch)

        result = _resolve("/opt/ffmpeg", "/opt/ffprobe")

        assert result == FFmpegBinaries(ffmpeg="/opt/ffmpeg", ffprobe="/opt/ffprobe")

    def test_invalid_configured_path_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A configured path that is not executable raises a clear error."""
        monkeypatch.setattr(ffmpeg_utils.shutil, "which", _fake_which({}))

        with pytest.raises(FileNotFoundError, match="ffmpeg_path"):
            _resolve("/nope/ffmpeg", None)

    def test_invalid_configured_ffprobe_path_names_ffprobe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A bad ffprobe path names ffprobe (not ffmpeg) in the error, from the shared validator."""
        monkeypatch.setattr(ffmpeg_utils.shutil, "which", _fake_which({}))

        with pytest.raises(FileNotFoundError, match=r"ffprobe binary.*ffprobe_path"):
            _resolve(None, "/nope/ffprobe")


class TestResolvePublicApi:
    """The public entry point reads settings and delegates to the resolver."""

    @pytest.mark.usefixtures("griptape_nodes")
    def test_reads_settings_and_resolves_from_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no configured paths, the public API resolves from PATH."""
        monkeypatch.setattr(
            ffmpeg_utils.shutil,
            "which",
            _fake_which({"ffmpeg": "/usr/bin/ffmpeg", "ffprobe": "/usr/bin/ffprobe"}),
        )

        result = resolve_ffmpeg_binaries()

        assert result == FFmpegBinaries(ffmpeg="/usr/bin/ffmpeg", ffprobe="/usr/bin/ffprobe")
