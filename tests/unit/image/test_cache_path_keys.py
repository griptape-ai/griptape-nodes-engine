"""Cache invalidation uses the same filename spellings as native image reads."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from griptape_nodes.image.cache import invalidate_image_cache


class TestCachePathKeys:
    def test_invalidation_includes_os_io_filename(self, tmp_path: Path) -> None:
        # Given an OS spelling distinct from the canonical identity, as on Windows.
        path = tmp_path / "source.png"
        io_path = tmp_path / "io-spelling" / "source.png"
        cache = MagicMock()
        with (
            patch("griptape_nodes.image.cache.oiio.ImageCache", return_value=cache),
            patch("griptape_nodes.image.cache.canonicalize_for_io", return_value=io_path, create=True),
        ):
            # When a plain source path is invalidated.
            invalidate_image_cache(path)

        # Then the exact filename used by read_cached_file is evicted too.
        cache.invalidate.assert_any_call(str(io_path), True)
