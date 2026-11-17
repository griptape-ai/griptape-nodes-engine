"""Various utility functions."""

from griptape_nodes.files.path_utils import resolve_workspace_path
from griptape_nodes.utils.async_utils import call_function
from griptape_nodes.utils.ffmpeg_cache import redirect_ffmpeg_cache, resolve_ffmpeg_directory
from griptape_nodes.utils.url_utils import get_content_type_from_extension


def install_file_url_support() -> None:
    """No-op kept so published griptape-nodes-app <=0.100.0 can import it. Remove once app drops the call."""


__all__ = [
    "call_function",
    "get_content_type_from_extension",
    "install_file_url_support",
    "redirect_ffmpeg_cache",
    "resolve_ffmpeg_directory",
    "resolve_workspace_path",
]
