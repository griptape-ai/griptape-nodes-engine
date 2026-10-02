"""Example node for the Node Development Guide: reporting failures with `NodeError`.

Pick a failure from the "Failure" dropdown and run the node to see how each one appears in the
editor's error panel. Nothing here calls a real service. The responses are canned, shaped like the
ones real APIs return.

- "Provider failure": the service accepted the job and reported it failed. Raises `NodeError` with
  labelled fields, the response body, and a link.
- "Service rejected the request": an HTTP call raised. Catches the SDK's exception and raises
  `NodeError` from it, with the status code, request ID, and response body attached.
- "Unsupported format": the input can't be used. Raises `NodeError` with links that explain the fix.
- "Missing API key": checked before the node runs, using the real secrets lookup. Fails unless
  `EXAMPLE_SERVICE_API_KEY` is set.
- "Missing preset setting": raises a plain `KeyError`. The editor still shows its message and type.
- "Validation problems": returns two exceptions from `validate_before_node_run`, so the node never
  runs and the editor lists both problems.

Copy this file into your sandbox library folder to try it in the editor.
"""

from __future__ import annotations

from typing import Any, NoReturn
from urllib.parse import quote

import httpx

from griptape_nodes.exe_types.core_types import NodeError, NodeErrorLink, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode
from griptape_nodes.exe_types.param_types.parameter_string import ParameterString
from griptape_nodes.retained_mode.events.secrets_events import GetSecretValueRequest, GetSecretValueResultSuccess
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.traits.options import Options

PROVIDER_FAILURE = "Provider failure"
SERVICE_REJECTED = "Service rejected the request"
UNSUPPORTED_FORMAT = "Unsupported format"
MISSING_API_KEY = "Missing API key"
MISSING_PRESET_SETTING = "Missing preset setting"
VALIDATION_PROBLEMS = "Validation problems"

ERROR_HANDLING_GUIDE = NodeErrorLink(
    label="Error handling guide",
    url="https://docs.griptapenodes.com/development/custom_nodes/error_handling/",
)


class ExampleNodeErrorNode(DataNode):
    API_KEY_NAME = "EXAMPLE_SERVICE_API_KEY"

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)

        self.add_parameter(
            ParameterString(
                name="failure",
                tooltip="Which failure to simulate when the node runs.",
                default_value=PROVIDER_FAILURE,
                allowed_modes={ParameterMode.PROPERTY},
                traits={
                    Options(
                        choices=[
                            PROVIDER_FAILURE,
                            SERVICE_REJECTED,
                            UNSUPPORTED_FORMAT,
                            MISSING_API_KEY,
                            MISSING_PRESET_SETTING,
                            VALIDATION_PROBLEMS,
                        ]
                    )
                },
            )
        )

    def validate_before_node_run(self) -> list[Exception] | None:
        # Catch what you can before the node runs, so nothing half-runs and the user can fix every
        # problem at once.
        failure = self.get_parameter_value("failure")
        if failure == MISSING_API_KEY:
            return self._check_api_key()
        if failure == VALIDATION_PROBLEMS:
            # One exception per problem. The editor lists each on its own line. Don't start
            # messages with the node's name: the editor already shows which node failed.
            return [
                ValueError("Connect an image to 'Input Image'."),
                ValueError("'Prompt' is empty. Describe the edit you want."),
            ]
        return None

    def process(self) -> None:
        failure = self.get_parameter_value("failure")
        if failure == SERVICE_REJECTED:
            self._send_request()
        elif failure == UNSUPPORTED_FORMAT:
            self._raise_unsupported_format()
        elif failure == MISSING_PRESET_SETTING:
            self._raise_missing_preset_setting()
        else:
            self._raise_provider_failure()

    def _check_api_key(self) -> list[Exception] | None:
        # Read secrets with a request, not the secrets manager: the request also works when the
        # node runs in a worker. `should_error_on_not_found=False` returns None for a missing key
        # instead of logging an error, because reporting it is this method's job.
        result = GriptapeNodes.handle_request(
            GetSecretValueRequest(key=self.API_KEY_NAME, should_error_on_not_found=False)
        )
        if isinstance(result, GetSecretValueResultSuccess) and result.value:
            return None
        # Name the exact setting and where to find it, so the message still helps in logs. A
        # validation exception can be a NodeError too, so it can carry a link. A link starting with
        # "#" opens a place in the editor: this one opens API Keys & Secrets filtered to the key.
        msg = f"{self.API_KEY_NAME} is not set. Add it in Settings → API Keys & Secrets, then run the node again."
        return [
            NodeError(
                msg,
                # URL-encode values in the query, so a key name with "&" or "#" can't break the link.
                links=[
                    NodeErrorLink(label="Add the API key", url=f"#settings-secrets?filter={quote(self.API_KEY_NAME)}")
                ],
            )
        ]

    def _send_request(self) -> None:
        try:
            self._call_service()
        except httpx.HTTPStatusError as e:
            # Raise from the original so its traceback stays in the logs. Use the provider's own
            # explanation as the message, and attach what support needs instead of pasting the
            # whole error text in.
            body = e.response.json()
            error = body["error"]
            fields = {"status_code": e.response.status_code, "error_code": error["code"], "parameter": error["param"]}
            request_id = e.response.headers.get("x-request-id")
            if request_id is not None:
                fields["request_id"] = request_id
            raise NodeError(error["message"], fields=fields, response=body) from e

    def _call_service(self) -> None:
        """Stand-in for a real call such as ``client.post(...)``: the 400 a chat API sends for a bad parameter."""
        request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
        response = httpx.Response(
            400,
            request=request,
            headers={"x-request-id": "req_8f2c41d07a9e4b15"},
            json={
                "error": {
                    "message": "Unsupported parameter: 'max_tokens' is not supported with this model. "
                    "Use 'max_completion_tokens' instead.",
                    "type": "invalid_request_error",
                    "param": "max_tokens",
                    "code": "unsupported_parameter",
                }
            },
        )
        response.raise_for_status()

    def _raise_provider_failure(self) -> NoReturn:
        response = self._fake_provider_response()
        detail = response["status_detail"]["details"]
        # Put the reason in the message, and attach the rest instead of pasting the response in.
        # Remove base64 image data from a response before attaching it. A response over 16 KB is
        # dropped, and the user only sees that there was one.
        msg = f"The image service could not process the request: {detail}."
        raise NodeError(
            msg,
            fields={"generation_id": response["generation_id"], "status": response["status"]},
            response=response,
            links=[ERROR_HANDLING_GUIDE],
        )

    def _raise_unsupported_format(self) -> NoReturn:
        # Links point to pages that explain how to fix the problem. The editor shows them apart from
        # the message, so the message stays readable in logs too. Up to three, http or https, or "#"
        # links into the editor (see the missing API key case).
        msg = "Image format 'image/heic' is not supported. Convert the image to PNG or JPEG and try again."
        raise NodeError(
            msg,
            links=[
                NodeErrorLink(
                    label="Supported image formats",
                    url="https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html",
                ),
                ERROR_HANDLING_GUIDE,
            ],
        )

    def _raise_missing_preset_setting(self) -> NoReturn:
        # A plain exception still reaches the editor with its message and type. The engine removes
        # the quotes Python puts around a KeyError's message.
        msg = "The preset has no 'strength' setting. Pick another preset."
        raise KeyError(msg)

    def _fake_provider_response(self) -> dict[str, Any]:
        return {
            "generation_id": "90dbcfa0-ac4b-4feb-b234-0badff151ee2",
            "model_id": "example-enhance",
            "status": "ERRORED",
            "status_detail": {"error": "client error", "details": "proxy client error"},
        }
