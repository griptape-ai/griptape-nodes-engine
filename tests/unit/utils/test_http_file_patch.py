"""Unit tests for http_file_patch."""

from pathlib import Path
from types import ModuleType

import httpx
import httpx2
import pytest
import requests

from griptape_nodes.utils import http_file_patch
from griptape_nodes.utils.http_file_patch import install_file_url_support

HTTP_MODULES = [httpx, httpx2]


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the patches, restoring the libraries afterwards."""
    for module in HTTP_MODULES:
        for name in ("request", "get", "post", "put", "delete", "patch"):
            monkeypatch.setattr(module, name, getattr(module, name))
        monkeypatch.setattr(module.Client, "request", module.Client.request)
        monkeypatch.setattr(module.AsyncClient, "request", module.AsyncClient.request)
    monkeypatch.setattr(requests, "get", requests.get)
    monkeypatch.setattr(http_file_patch, "_patches_installed", False)
    install_file_url_support()


@pytest.fixture
def local_file(tmp_path: Path) -> Path:
    """A text file containing `hello`."""
    path = tmp_path / "hello.txt"
    path.write_text("hello")
    return path


@pytest.mark.usefixtures("installed")
@pytest.mark.parametrize("module", HTTP_MODULES)
class TestHttpxModules:
    def test_module_get_reads_file_url(self, module: ModuleType, local_file: Path) -> None:
        response = module.get(local_file.as_uri())

        assert response.status_code == http_file_patch.HTTP_OK
        assert response.text == "hello"

    def test_module_get_reads_local_path(self, module: ModuleType, local_file: Path) -> None:
        assert module.get(str(local_file)).text == "hello"

    def test_client_get_reads_file_url(self, module: ModuleType, local_file: Path) -> None:
        with module.Client() as client:
            assert client.get(local_file.as_uri()).text == "hello"

    @pytest.mark.asyncio
    async def test_async_client_get_reads_file_url(self, module: ModuleType, local_file: Path) -> None:
        async with module.AsyncClient() as client:
            response = await client.get(local_file.as_uri())

        assert response.text == "hello"

    def test_missing_file_raises_the_callers_error_type(self, module: ModuleType, tmp_path: Path) -> None:
        response = module.get((tmp_path / "missing.txt").as_uri())

        with pytest.raises(module.HTTPStatusError):
            response.raise_for_status()


@pytest.mark.usefixtures("installed")
def test_requests_get_reads_file_url(local_file: Path) -> None:
    """The `requests` patch keeps working alongside httpx and httpx2."""
    assert requests.get(local_file.as_uri(), timeout=1).text == "hello"
