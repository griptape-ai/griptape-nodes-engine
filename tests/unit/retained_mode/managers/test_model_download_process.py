import sys
from unittest.mock import MagicMock, patch

import pytest
from huggingface_hub.errors import HFValidationError

from griptape_nodes.retained_mode.managers import model_download_process
from griptape_nodes.utils.model_download_errors import DownloadErrorKind, parse_error_event


def _run(argv: list[str], model_manager: MagicMock) -> None:
    with (
        patch.object(sys, "argv", ["model_download_process", *argv]),
        patch.object(model_download_process.GriptapeNodes, "ModelManager", return_value=model_manager),
    ):
        model_download_process.main()


class TestMain:
    def test_passes_arguments_to_download_model(self) -> None:
        model_manager = MagicMock()

        _run(["org/model", "--local-dir", "models", "--revision", "v1"], model_manager)

        model_manager.download_model.assert_called_once_with(
            model_id="org/model",
            local_dir="models",
            revision="v1",
            allow_patterns=None,
            ignore_patterns=None,
        )

    def test_failure_writes_verdict_to_stderr_and_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        model_manager = MagicMock()
        model_manager.download_model.side_effect = HFValidationError("bad id")

        with pytest.raises(SystemExit) as exc_info:
            _run(["not valid!!"], model_manager)

        assert exc_info.value.code == 1
        failure = parse_error_event(capsys.readouterr().err)
        assert failure is not None
        assert failure.kind is DownloadErrorKind.INVALID_MODEL_ID
