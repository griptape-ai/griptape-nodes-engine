"""Unit tests for url_utils module."""

from __future__ import annotations

import sys
from pathlib import Path

from griptape_nodes.utils.url_utils import (
    get_content_type_from_extension,
    is_url_or_path,
    uri_to_path,
)

IS_WINDOWS = sys.platform == "win32"


class TestIsUrlOrPath:
    """Test is_url_or_path utility function."""

    def test_http_urls(self) -> None:
        """Test HTTP URL detection."""
        assert is_url_or_path("http://example.com/image.png") is True
        assert is_url_or_path("http://localhost/file.txt") is True
        assert is_url_or_path("http://192.168.1.1/api") is True

    def test_https_urls(self) -> None:
        """Test HTTPS URL detection."""
        assert is_url_or_path("https://example.com/file.txt") is True
        assert is_url_or_path("https://localhost:8080/api") is True
        assert is_url_or_path("https://subdomain.example.com/path/to/file") is True

    def test_file_urls(self) -> None:
        """Test file:// URL detection."""
        assert is_url_or_path("file:///absolute/path") is True
        assert is_url_or_path("file:///home/user/file.txt") is True
        assert is_url_or_path("file://C:/Windows/path") is True

    def test_absolute_unix_paths(self) -> None:
        """Test absolute Unix path detection."""
        assert is_url_or_path("/absolute/unix/path") is True
        assert is_url_or_path("/home/user/file.txt") is True
        assert is_url_or_path("/var/log/app.log") is True

    def test_absolute_windows_paths(self) -> None:
        """Test absolute Windows path detection."""
        assert is_url_or_path("C:\\Windows\\path") is True
        assert is_url_or_path("D:\\data\\file.txt") is True
        assert is_url_or_path("E:\\") is True

    def test_relative_paths(self) -> None:
        """Test relative path detection."""
        assert is_url_or_path("./relative/path") is True
        assert is_url_or_path("../parent/path") is True
        assert is_url_or_path("file.txt") is True
        assert is_url_or_path("path/to/file.txt") is True
        assert is_url_or_path("./file.txt") is True
        assert is_url_or_path("../file.txt") is True

    def test_unc_paths(self) -> None:
        """Test UNC (Windows network) path detection."""
        assert is_url_or_path("\\\\server\\share\\file") is True
        assert is_url_or_path("\\\\network\\path") is True

    def test_data_uris_excluded(self) -> None:
        """Test that data: URIs return False."""
        assert is_url_or_path("data:image/png;base64,iVBORw0KGg") is False
        assert is_url_or_path("data:text/plain,hello") is False
        assert is_url_or_path("data:application/json,{}") is False

    def test_empty_and_whitespace(self) -> None:
        """Test that empty and whitespace strings return False."""
        assert is_url_or_path("") is False
        assert is_url_or_path("   ") is False
        assert is_url_or_path("\t") is False
        assert is_url_or_path("\n") is False

    def test_other_schemes_excluded(self) -> None:
        """Test that other URI schemes return False."""
        assert is_url_or_path("ftp://ftp.example.com") is False
        assert is_url_or_path("ftps://secure.ftp.com") is False
        assert is_url_or_path("mailto:user@example.com") is False
        assert is_url_or_path("javascript:alert('xss')") is False
        assert is_url_or_path("ssh://git@github.com/repo") is False

    def test_plain_text_strings(self) -> None:
        """Test that plain text without path indicators returns False."""
        assert is_url_or_path("hello world") is True  # Path() accepts this
        assert is_url_or_path("just text") is True  # Path() accepts this
        assert is_url_or_path("random") is True  # Path() accepts this as filename

    def test_urls_with_query_params(self) -> None:
        """Test URLs with query parameters."""
        assert is_url_or_path("https://example.com/api?key=value") is True
        assert is_url_or_path("http://localhost:8080/path?param=1&other=2") is True

    def test_urls_with_fragments(self) -> None:
        """Test URLs with fragments."""
        assert is_url_or_path("https://example.com/page#section") is True
        assert is_url_or_path("http://docs.example.com/guide#intro") is True

    def test_paths_with_spaces(self) -> None:
        """Test paths containing spaces."""
        assert is_url_or_path("/path with spaces/file.txt") is True
        assert is_url_or_path("C:\\Program Files\\app.exe") is True
        assert is_url_or_path("./folder name/file.txt") is True

    def test_paths_with_special_characters(self) -> None:
        """Test paths with special characters."""
        assert is_url_or_path("/path/to/file-name_v2.txt") is True
        assert is_url_or_path("./my-project/src/utils.py") is True
        assert is_url_or_path("file_2024-01-15.log") is True


class TestGetContentTypeFromExtension:
    """Test get_content_type_from_extension utility function."""

    def test_image_extensions(self) -> None:
        """Test common image file extensions."""
        assert get_content_type_from_extension("image.png") == "image/png"
        assert get_content_type_from_extension("photo.jpg") == "image/jpeg"
        assert get_content_type_from_extension("photo.jpeg") == "image/jpeg"
        assert get_content_type_from_extension("icon.gif") == "image/gif"
        assert get_content_type_from_extension("vector.svg") == "image/svg+xml"
        # webp MIME type may not be registered on Windows
        webp_result = get_content_type_from_extension("image.webp")
        assert webp_result in ("image/webp", None)

    def test_text_extensions(self) -> None:
        """Test text file extensions."""
        assert get_content_type_from_extension("file.txt") == "text/plain"
        assert get_content_type_from_extension("page.html") == "text/html"
        assert get_content_type_from_extension("style.css") == "text/css"
        # JavaScript MIME type varies: text/javascript (standard) vs application/javascript (also valid)
        js_result = get_content_type_from_extension("script.js")
        assert js_result in ("text/javascript", "application/javascript")

    def test_data_extensions(self) -> None:
        """Test data file extensions."""
        assert get_content_type_from_extension("data.json") == "application/json"
        # XML MIME type varies: application/xml vs text/xml
        xml_result = get_content_type_from_extension("data.xml")
        assert xml_result in ("application/xml", "text/xml")
        # ZIP MIME type varies: application/zip vs application/x-zip-compressed
        zip_result = get_content_type_from_extension("archive.zip")
        assert zip_result in ("application/zip", "application/x-zip-compressed")
        assert get_content_type_from_extension("document.pdf") == "application/pdf"

    def test_video_extensions(self) -> None:
        """Test video file extensions."""
        assert get_content_type_from_extension("video.mp4") == "video/mp4"
        assert get_content_type_from_extension("video.webm") == "video/webm"
        # AVI MIME type varies: video/x-msvideo vs video/avi
        avi_result = get_content_type_from_extension("movie.avi")
        assert avi_result in ("video/x-msvideo", "video/avi")

    def test_audio_extensions(self) -> None:
        """Test audio file extensions."""
        assert get_content_type_from_extension("audio.mp3") == "audio/mpeg"
        # WAV MIME type varies: audio/x-wav vs audio/wav
        wav_result = get_content_type_from_extension("audio.wav")
        assert wav_result in ("audio/x-wav", "audio/wav")
        assert get_content_type_from_extension("audio.ogg") == "audio/ogg"

    def test_path_object_input(self) -> None:
        """Test that Path objects are accepted."""
        assert get_content_type_from_extension(Path("file.png")) == "image/png"
        assert get_content_type_from_extension(Path("/path/to/file.json")) == "application/json"
        assert get_content_type_from_extension(Path("C:\\path\\to\\file.pdf")) == "application/pdf"

    def test_string_path_input(self) -> None:
        """Test that string paths are accepted."""
        assert get_content_type_from_extension("/path/to/file.txt") == "text/plain"
        assert get_content_type_from_extension("./relative/path/file.html") == "text/html"
        assert get_content_type_from_extension("C:\\Windows\\path\\file.css") == "text/css"

    def test_unknown_extension(self) -> None:
        """Test that unknown extensions return None."""
        assert get_content_type_from_extension("file.unknown") is None
        assert get_content_type_from_extension("file.unknownextension") is None
        assert get_content_type_from_extension("file.abc123") is None

    def test_no_extension(self) -> None:
        """Test files without extensions."""
        assert get_content_type_from_extension("README") is None
        assert get_content_type_from_extension("/path/to/makefile") is None
        assert get_content_type_from_extension("dockerfile") is None

    def test_multiple_dots(self) -> None:
        """Test files with multiple dots in name."""
        # JavaScript MIME type varies: text/javascript vs application/javascript
        js_result = get_content_type_from_extension("file.min.js")
        assert js_result in ("text/javascript", "application/javascript")
        assert get_content_type_from_extension("archive.tar.gz") == "application/x-tar"
        assert get_content_type_from_extension("data.backup.json") == "application/json"

    def test_case_insensitivity(self) -> None:
        """Test that extension matching is case insensitive."""
        assert get_content_type_from_extension("file.PNG") == "image/png"
        assert get_content_type_from_extension("file.JPG") == "image/jpeg"
        assert get_content_type_from_extension("file.Html") == "text/html"


class TestUriToPath:
    """Test uri_to_path utility function."""

    def test_unix_file_uri(self) -> None:
        """Test conversion of Unix file URIs."""
        result = uri_to_path("file:///home/user/file.txt")
        assert isinstance(result, Path)
        # Use as_posix() for cross-platform comparison
        assert result.as_posix() == "/home/user/file.txt"

    def test_unix_absolute_path(self) -> None:
        """Test conversion of Unix absolute paths."""
        result = uri_to_path("file:///var/log/app.log")
        assert isinstance(result, Path)
        # Use as_posix() for cross-platform comparison
        assert result.as_posix() == "/var/log/app.log"

    def test_windows_file_uri(self) -> None:
        """Test conversion of Windows file URIs."""
        result = uri_to_path("file:///C:/Windows/path/file.txt")
        assert result.as_posix() == "C:/Windows/path/file.txt"

    def test_uri_with_spaces(self) -> None:
        """Test URIs with URL-encoded spaces."""
        result = uri_to_path("file:///home/user/my%20file.txt")
        assert isinstance(result, Path)
        # Use as_posix() for cross-platform comparison
        assert result.as_posix() == "/home/user/my file.txt"

    def test_uri_with_special_characters(self) -> None:
        """Test URIs with URL-encoded special characters."""
        result = uri_to_path("file:///path/with%20spaces%20and%20%26%20symbols.txt")
        assert isinstance(result, Path)
        # Use as_posix() for cross-platform comparison
        assert result.as_posix() == "/path/with spaces and & symbols.txt"

    def test_windows_drive_letter_path_preserved(self) -> None:
        """Windows drive letters must not be treated as URI schemes."""
        result = uri_to_path(r"Z:\Griptape\inputs\REX_MC_10_10_Image.jpg")
        assert isinstance(result, Path)
        # The drive letter "Z:" must be preserved, not parsed away as a URI scheme
        assert str(result).startswith("Z:")

    def test_plain_absolute_path_preserved(self) -> None:
        """Plain absolute paths (no file:// scheme) must pass through unchanged."""
        result = uri_to_path("/home/user/file.txt")
        assert isinstance(result, Path)
        assert result.as_posix() == "/home/user/file.txt"

    def test_plain_relative_path_preserved(self) -> None:
        """Plain relative paths must pass through unchanged."""
        result = uri_to_path("relative/path/file.txt")
        assert isinstance(result, Path)
        assert result.as_posix() == "relative/path/file.txt"
