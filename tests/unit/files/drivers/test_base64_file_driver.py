"""Tests for Base64FileDriver."""

import base64

import pytest

from griptape_nodes.files.drivers.base64_file_driver import Base64FileDriver

# Constants
EXPECTED_PRIORITY = 90
LARGE_DATA_SIZE = 10000


class TestBase64FileDriver:
    """Test suite for Base64FileDriver."""

    @pytest.fixture
    def driver(self) -> Base64FileDriver:
        """Create a Base64FileDriver instance."""
        return Base64FileDriver()

    def test_priority(self, driver: Base64FileDriver) -> None:
        """Test priority is 90 (before LocalFileDriver at 100)."""
        assert driver.priority == EXPECTED_PRIORITY

    def test_can_handle_valid_base64(self, driver: Base64FileDriver) -> None:
        """Test can_handle accepts valid base64 strings."""
        valid_base64 = base64.b64encode(b"Hello, World! This is a test.").decode("utf-8")
        assert driver.can_handle(valid_base64)

    def test_can_handle_valid_base64_with_padding(self, driver: Base64FileDriver) -> None:
        """Test can_handle accepts base64 with padding."""
        # "test" encodes to "dGVzdA==" which has padding
        valid_base64 = base64.b64encode(b"test" * 10).decode("utf-8")
        assert driver.can_handle(valid_base64)

    def test_can_handle_rejects_base64_with_trailing_whitespace(self, driver: Base64FileDriver) -> None:
        """Test can_handle rejects base64 with trailing whitespace."""
        # The regex pattern requires base64 to end with optional '=' padding
        # Trailing whitespace is not accepted
        valid_base64 = base64.b64encode(b"Hello, World! This is a test.").decode("utf-8")
        valid_base64_with_trailing_space = f"{valid_base64}\n"
        assert not driver.can_handle(valid_base64_with_trailing_space)

    def test_can_handle_rejects_too_short(self, driver: Base64FileDriver) -> None:
        """Test can_handle rejects strings shorter than minimum length."""
        short_string = "SGVsbG8="  # "Hello" in base64 - only 8 chars
        assert not driver.can_handle(short_string)

    def test_can_handle_rejects_data_uri(self, driver: Base64FileDriver) -> None:
        """Test can_handle rejects data: URIs."""
        data_uri = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAUA"
        assert not driver.can_handle(data_uri)

    def test_can_handle_rejects_http_url(self, driver: Base64FileDriver) -> None:
        """Test can_handle rejects http:// URLs."""
        http_url = "http://example.com/image.png"
        assert not driver.can_handle(http_url)

    def test_can_handle_rejects_https_url(self, driver: Base64FileDriver) -> None:
        """Test can_handle rejects https:// URLs."""
        https_url = "https://example.com/image.png"
        assert not driver.can_handle(https_url)

    def test_can_handle_rejects_file_uri(self, driver: Base64FileDriver) -> None:
        """Test can_handle rejects file:// URIs."""
        file_uri = "file:///path/to/file.txt"
        assert not driver.can_handle(file_uri)

    def test_can_handle_rejects_absolute_path(self, driver: Base64FileDriver) -> None:
        """Test can_handle rejects absolute file paths."""
        absolute_path = "/absolute/path/to/file.txt"
        assert not driver.can_handle(absolute_path)

    def test_can_handle_rejects_relative_path(self, driver: Base64FileDriver) -> None:
        """Test can_handle rejects relative file paths."""
        relative_path = "./relative/path/to/file.txt"
        assert not driver.can_handle(relative_path)

    def test_can_handle_rejects_path_with_slash_no_padding(self, driver: Base64FileDriver) -> None:
        """Test can_handle rejects path-like strings with slashes but no padding."""
        path_like = "some/path/to/file.txt"
        assert not driver.can_handle(path_like)

    def test_can_handle_accepts_base64_with_slash_and_padding(self, driver: Base64FileDriver) -> None:
        """Test can_handle accepts base64 that contains slashes and ends with padding."""
        valid_with_slash = "aGVsbG8/PyB3b3JsZCwgdGhpcyBpcyBhIHRlc3Q="
        assert driver.can_handle(valid_with_slash)

    def test_can_handle_rejects_invalid_characters(self, driver: Base64FileDriver) -> None:
        """Test can_handle rejects strings with invalid base64 characters."""
        invalid_string = "This is not base64! @#$%^&*()"
        assert not driver.can_handle(invalid_string)

    def test_can_handle_rejects_invalid_base64_decode_fails(self, driver: Base64FileDriver) -> None:
        """Test can_handle rejects strings that look like base64 but fail to decode."""
        # Valid characters, but the length is not a multiple of 4.
        invalid_base64 = "A" * 30
        assert not driver.can_handle(invalid_base64)

    @pytest.mark.asyncio
    async def test_read_valid_base64(self, driver: Base64FileDriver) -> None:
        """Test read decodes valid base64 to bytes."""
        original_data = b"Hello, World! This is test data."
        base64_string = base64.b64encode(original_data).decode("utf-8")

        result = await driver.read(base64_string, timeout=5.0)

        assert result == original_data

    @pytest.mark.asyncio
    async def test_read_ignores_timeout(self, driver: Base64FileDriver) -> None:
        """Test read works regardless of timeout value (no network operation)."""
        original_data = b"Test data"
        base64_string = base64.b64encode(original_data).decode("utf-8")

        result = await driver.read(base64_string, timeout=0.001)

        assert result == original_data

    @pytest.mark.asyncio
    async def test_read_invalid_base64_raises_value_error(self, driver: Base64FileDriver) -> None:
        """Test read raises ValueError for invalid base64."""
        invalid_base64 = "This is not valid base64!@#$"

        with pytest.raises(ValueError, match="Failed to decode raw base64 data"):
            await driver.read(invalid_base64, timeout=5.0)

    @pytest.mark.asyncio
    async def test_read_empty_base64(self, driver: Base64FileDriver) -> None:
        """Test read handles empty base64 string."""
        empty_base64 = base64.b64encode(b"").decode("utf-8")

        result = await driver.read(empty_base64, timeout=5.0)

        assert result == b""

    @pytest.mark.asyncio
    async def test_exists_valid_base64(self, driver: Base64FileDriver) -> None:
        """Test exists returns True for valid base64."""
        valid_base64 = base64.b64encode(b"Test data").decode("utf-8")

        result = await driver.exists(valid_base64)

        assert result is True

    @pytest.mark.asyncio
    async def test_exists_invalid_base64(self, driver: Base64FileDriver) -> None:
        """Test exists returns False for invalid base64."""
        invalid_base64 = "Not valid base64!@#$"

        result = await driver.exists(invalid_base64)

        assert result is False

    @pytest.mark.asyncio
    async def test_exists_malformed_base64(self, driver: Base64FileDriver) -> None:
        """Test exists returns False for malformed base64."""
        malformed_base64 = "ABC"  # Too short, invalid padding

        result = await driver.exists(malformed_base64)

        assert result is False

    def test_get_size_valid_base64(self, driver: Base64FileDriver) -> None:
        """Test get_size returns correct decoded size."""
        original_data = b"Hello, World!"
        base64_string = base64.b64encode(original_data).decode("utf-8")

        size = driver.get_size(base64_string)

        assert size == len(original_data)

    def test_get_size_empty_base64(self, driver: Base64FileDriver) -> None:
        """Test get_size returns 0 for empty data."""
        empty_base64 = base64.b64encode(b"").decode("utf-8")

        size = driver.get_size(empty_base64)

        assert size == 0

    def test_get_size_invalid_base64_raises_value_error(self, driver: Base64FileDriver) -> None:
        """Test get_size raises ValueError for invalid base64."""
        invalid_base64 = "Not valid base64!@#$"

        with pytest.raises(ValueError, match="Failed to decode raw base64 data"):
            driver.get_size(invalid_base64)

    def test_get_size_large_data(self, driver: Base64FileDriver) -> None:
        """Test get_size handles large data correctly."""
        original_data = b"x" * LARGE_DATA_SIZE
        base64_string = base64.b64encode(original_data).decode("utf-8")

        size = driver.get_size(base64_string)

        assert size == LARGE_DATA_SIZE
