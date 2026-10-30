"""Tests for how the HuggingFace dropdown treats a model whose download has not completed.

HuggingFace creates a cache entry as soon as a download starts, so a paused or failed model is
listed by the cache scan just like a complete one. The download status is what tells them apart.
"""

from collections.abc import Iterator
from unittest.mock import patch

import pytest

from griptape_nodes.exe_types.param_components.huggingface.huggingface_repo_parameter import HuggingFaceRepoParameter
from griptape_nodes.retained_mode.events.model_events import (
    ListModelDownloadsRequest,
    ListModelDownloadsResultSuccess,
    ModelDownloadStatus,
)
from tests.unit.exe_types.mocks import MockNode

REPO = "black-forest-labs/FLUX.1-schnell"
REVISION = "a" * 40


def _status(status: str) -> ModelDownloadStatus:
    return ModelDownloadStatus(model_id=REPO, status=status, started_at="", updated_at="")


@pytest.fixture
def download_status() -> Iterator[list[str]]:
    """Cache holds REPO; the download list reports whatever status the test appends."""
    statuses: list[str] = []

    def handle_request(request: object) -> object:
        if isinstance(request, ListModelDownloadsRequest):
            return ListModelDownloadsResultSuccess(downloads=[_status(s) for s in statuses], result_details="ok")
        msg = f"unexpected request: {type(request).__name__}"
        raise AssertionError(msg)

    module = "griptape_nodes.exe_types.param_components.huggingface.huggingface_repo_parameter"
    with (
        patch(f"{module}.list_repo_revisions_in_cache", return_value=[(REPO, REVISION)]),
        patch("griptape_nodes.retained_mode.engine.Engine.handle_request", side_effect=handle_request),
    ):
        yield statuses


def _param() -> HuggingFaceRepoParameter:
    param = HuggingFaceRepoParameter(MockNode(), repo_ids=[REPO], gated=False)
    param.add_input_parameters()
    param.refresh_parameters()
    return param


def _download_button_hidden(param: HuggingFaceRepoParameter) -> bool:
    button = param._node.get_parameter_by_name("model_download")
    assert button is not None
    return button.hide


def _row(param: HuggingFaceRepoParameter) -> dict:
    (row,) = param._build_data_choices([REPO])
    return row


@pytest.mark.parametrize(
    ("status", "subtitle"),
    [("downloading", "Downloading…"), ("paused", "Paused"), ("failed", "Download failed")],
)
def test_incomplete_download_is_not_shown_as_downloaded(download_status: list[str], status: str, subtitle: str) -> None:
    """The row names the status and the download button stays visible."""
    download_status.append(status)
    param = _param()
    assert _row(param)["subtitle"] == subtitle
    assert _download_button_hidden(param) is False


@pytest.mark.parametrize("status", ["downloading", "paused", "failed"])
def test_incomplete_download_blocks_the_run(download_status: list[str], status: str) -> None:
    """The node refuses to run on files that may be partial."""
    download_status.append(status)
    errors = _param().validate_before_node_run()
    assert errors is not None
    assert "has not completed" in str(errors[0])


@pytest.mark.parametrize("statuses", [[], ["completed"]])
def test_completed_download_runs(download_status: list[str], statuses: list[str]) -> None:
    """A cached model with no status record, or a completed one, is ready."""
    download_status.extend(statuses)
    param = _param()
    assert _row(param)["subtitle"] == "Downloaded"
    assert _download_button_hidden(param) is True
    assert param.validate_before_node_run() is None
