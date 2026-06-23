"""Tests for ModelManager methods added for model size support.

Covers:
- `on_handle_get_model_info_request` — token guard and HF API delegation
- `on_handle_search_models_request` — search result handling
- `on_handle_declare_model_invocation_request` — clears a declared invocation past the pre-dispatch chain
- `_download_model_task` — the spawned subprocess targets a runnable module
"""

import importlib.util
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from griptape_nodes.retained_mode.events.base_events import RequestPayload
from griptape_nodes.retained_mode.events.model_events import (
    DeclareModelInvocationRequest,
    DeclareModelInvocationResultFailure,
    DeclareModelInvocationResultSuccess,
    GetModelInfoRequest,
    GetModelInfoResultFailure,
    GetModelInfoResultSuccess,
    SearchModelsRequest,
    SearchModelsResultFailure,
    SearchModelsResultSuccess,
)
from griptape_nodes.retained_mode.managers.event_manager import EventManager
from griptape_nodes.retained_mode.managers.model_manager import DownloadParams, ModelManager


@pytest.fixture
def model_manager() -> ModelManager:
    """Bare ModelManager without event wiring."""
    return ModelManager.__new__(ModelManager)


# ---------------------------------------------------------------------------
# on_handle_get_model_info_request
# ---------------------------------------------------------------------------


class TestOnHandleGetModelInfoRequest:
    @pytest.mark.asyncio
    async def test_returns_failure_when_no_hf_token(self, model_manager: ModelManager) -> None:
        with patch(
            "griptape_nodes.retained_mode.managers.model_manager.get_token",
            return_value=None,
        ):
            result = await model_manager.on_handle_get_model_info_request(
                GetModelInfoRequest(model_id="microsoft/phi-2")
            )

        assert isinstance(result, GetModelInfoResultFailure)
        assert "No Hugging Face token found" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_returns_success_with_size_and_metadata(self, model_manager: ModelManager) -> None:
        expected_size = 11_125_567_216
        expected_downloads = 123_456
        expected_likes = 789
        fake_info = SimpleNamespace(
            used_storage=expected_size,
            safetensors=SimpleNamespace(parameters={"F16": 2_779_683_840}),
            author="microsoft",
            pipeline_tag="text-generation",
            library_name="transformers",
            tags=["pytorch"],
            downloads=expected_downloads,
            likes=expected_likes,
        )

        with (
            patch(
                "griptape_nodes.retained_mode.managers.model_manager.get_token",
                return_value="hf_token",
            ),
            patch(
                "griptape_nodes.retained_mode.managers.model_manager.hf_model_info",
                return_value=fake_info,
            ),
        ):
            result = await model_manager.on_handle_get_model_info_request(
                GetModelInfoRequest(model_id="microsoft/phi-2")
            )

        assert isinstance(result, GetModelInfoResultSuccess)
        assert result.model_id == "microsoft/phi-2"
        assert result.size_bytes == expected_size
        assert result.safetensors_parameters == {"F16": 2_779_683_840}
        assert result.author == "microsoft"
        assert result.task == "text-generation"
        assert result.library == "transformers"
        assert result.downloads == expected_downloads
        assert result.likes == expected_likes

    @pytest.mark.asyncio
    async def test_returns_failure_when_hf_api_raises(self, model_manager: ModelManager) -> None:
        with (
            patch(
                "griptape_nodes.retained_mode.managers.model_manager.get_token",
                return_value="hf_token",
            ),
            patch(
                "griptape_nodes.retained_mode.managers.model_manager.hf_model_info",
                side_effect=ValueError("model not found"),
            ),
        ):
            result = await model_manager.on_handle_get_model_info_request(GetModelInfoRequest(model_id="bad/model"))

        assert isinstance(result, GetModelInfoResultFailure)
        assert "bad/model" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_handles_missing_safetensors_gracefully(self, model_manager: ModelManager) -> None:
        fake_info = SimpleNamespace(
            used_storage=None,
            safetensors=None,
            author=None,
            pipeline_tag=None,
            library_name=None,
            tags=None,
            downloads=None,
            likes=None,
        )

        with (
            patch(
                "griptape_nodes.retained_mode.managers.model_manager.get_token",
                return_value="hf_token",
            ),
            patch(
                "griptape_nodes.retained_mode.managers.model_manager.hf_model_info",
                return_value=fake_info,
            ),
        ):
            result = await model_manager.on_handle_get_model_info_request(GetModelInfoRequest(model_id="some/model"))

        assert isinstance(result, GetModelInfoResultSuccess)
        assert result.size_bytes is None
        assert result.safetensors_parameters is None


# ---------------------------------------------------------------------------
# on_handle_search_models_request
# ---------------------------------------------------------------------------


class TestOnHandleSearchModelsRequest:
    def _make_hf_model(self, model_id: str) -> object:
        return SimpleNamespace(
            id=model_id,
            author=None,
            downloads=None,
            likes=None,
            created_at=None,
            last_modified=None,
            pipeline_tag=None,
            library_name=None,
            tags=None,
        )

    @pytest.mark.asyncio
    async def test_returns_success_with_model_list(self, model_manager: ModelManager) -> None:
        fake_model = self._make_hf_model("org/model")

        with patch(
            "griptape_nodes.retained_mode.managers.model_manager.list_models",
            return_value=[fake_model],
        ):
            result = await model_manager.on_handle_search_models_request(SearchModelsRequest(query="model"))

        assert isinstance(result, SearchModelsResultSuccess)
        assert len(result.models) == 1
        assert result.models[0].model_id == "org/model"

    @pytest.mark.asyncio
    async def test_returns_failure_when_list_models_raises(self, model_manager: ModelManager) -> None:
        with patch(
            "griptape_nodes.retained_mode.managers.model_manager.list_models",
            side_effect=RuntimeError("network error"),
        ):
            result = await model_manager.on_handle_search_models_request(SearchModelsRequest(query="model"))

        assert isinstance(result, SearchModelsResultFailure)


# ---------------------------------------------------------------------------
# on_handle_declare_model_invocation_request
# ---------------------------------------------------------------------------


class TestOnHandleDeclareModelInvocationRequest:
    def test_clears_the_node_to_proceed(self, model_manager: ModelManager) -> None:
        # Reaching the handler means the pre-dispatch chain did not deny the
        # declaration, so the node is cleared to invoke the model itself.
        result = model_manager.on_handle_declare_model_invocation_request(
            DeclareModelInvocationRequest(
                model_id="gtc_claude_opus_4_7",
                node_name="Agent_1",
            )
        )

        assert isinstance(result, DeclareModelInvocationResultSuccess)
        assert result.model_id == "gtc_claude_opus_4_7"

    def test_a_denying_pre_dispatch_hook_short_circuits_before_the_handler(self) -> None:
        # End to end: enforcement lives in the pre-dispatch chain, not the
        # handler. A hook that denies the declaration short-circuits with its
        # own failure; an allowed declaration reaches the handler and comes
        # back as a clear-to-proceed success. Policies gate the stable catalog
        # model key, the only handle the declaration carries.
        event_manager = EventManager()
        ModelManager(event_manager)

        def deny(request: RequestPayload, _context: object) -> DeclareModelInvocationResultFailure | None:
            if isinstance(request, DeclareModelInvocationRequest) and request.model_id == "blocked_model":
                return DeclareModelInvocationResultFailure(result_details="This model is blocked by your license.")
            return None

        event_manager.add_pre_dispatch_hook(deny)

        denied = event_manager.handle_request(DeclareModelInvocationRequest(model_id="blocked_model"))
        allowed = event_manager.handle_request(DeclareModelInvocationRequest(model_id="gtc_gpt_5"))

        assert isinstance(denied.result, DeclareModelInvocationResultFailure)
        assert "blocked by your license" in str(denied.result.result_details)
        # The allowed declaration reached the handler, which cleared it.
        assert isinstance(allowed.result, DeclareModelInvocationResultSuccess)
        assert allowed.result.model_id == "gtc_gpt_5"

    def test_authorization_checkpoint_denial_blocks_invocation(self) -> None:
        # The InvokeModel checkpoint gates the declared invocation: a denial from
        # a registered authorization hook turns into a failure so the node does
        # not invoke the model. The handler passes the stable catalog key; the app
        # resolves the provider and family from it.
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
            AuthorizationCheckpoint,
            CheckpointDenial,
            CheckpointFailure,
        )

        seen: dict[str, object] = {}

        def deny(checkpoint: AuthorizationCheckpoint) -> CheckpointDenial | None:
            seen["action"] = checkpoint.action
            seen["subject_id"] = checkpoint.subject_id
            seen["id"] = checkpoint.attributes.get("id")
            if checkpoint.subject_id == "gtc_claude_opus_4_7":
                return CheckpointDenial(failures=(CheckpointFailure(detail="Anthropic models are not enabled."),))
            return None

        GriptapeNodes.EventManager().add_authorization_hook(deny)
        manager = ModelManager.__new__(ModelManager)

        denied = manager.on_handle_declare_model_invocation_request(
            DeclareModelInvocationRequest(model_id="gtc_claude_opus_4_7")
        )
        assert isinstance(denied, DeclareModelInvocationResultFailure)
        assert "Anthropic models are not enabled." in str(denied.result_details)
        assert seen == {"action": "InvokeModel", "subject_id": "gtc_claude_opus_4_7", "id": "gtc_claude_opus_4_7"}

        allowed = manager.on_handle_declare_model_invocation_request(
            DeclareModelInvocationRequest(model_id="gtc_gpt_5")
        )
        assert isinstance(allowed, DeclareModelInvocationResultSuccess)

    def test_empty_failure_denial_still_yields_a_reason(self) -> None:
        # A hook that misuses the contract by returning a denial with no failures
        # (it should return None to allow) must not produce a reason-less message.
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
            AuthorizationCheckpoint,
            CheckpointDenial,
        )

        def deny(_checkpoint: AuthorizationCheckpoint) -> CheckpointDenial:
            return CheckpointDenial(failures=())

        GriptapeNodes.EventManager().add_authorization_hook(deny)
        manager = ModelManager.__new__(ModelManager)

        denied = manager.on_handle_declare_model_invocation_request(
            DeclareModelInvocationRequest(model_id="gtc_claude_opus_4_7")
        )
        assert isinstance(denied, DeclareModelInvocationResultFailure)
        assert "Denied by the license policy." in str(denied.result_details)


# ---------------------------------------------------------------------------
# _download_model_task — subprocess entry point
# ---------------------------------------------------------------------------


class TestDownloadModelTaskSubprocess:
    @pytest.mark.asyncio
    async def test_spawns_runnable_module(self, model_manager: ModelManager) -> None:
        """The download subprocess must target a module with a __main__ entry point.

        Regression guard for PR #4731, which removed the engine's top-level CLI
        entry point (`python -m griptape_nodes`) and left this subprocess invoking
        a module that no longer existed, breaking every Model Manager download.
        """
        model_manager._download_tasks = {}
        model_manager._download_processes = {}

        process = SimpleNamespace(
            stdout=None,
            stderr=None,
            returncode=0,
            wait=AsyncMock(return_value=0),
        )

        captured_cmd: list[str] = []

        async def fake_create_subprocess_exec(*cmd: str, **_kwargs: object) -> SimpleNamespace:
            captured_cmd.extend(cmd)
            return process

        with (
            patch("asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec),
            patch.object(model_manager, "_write_download_status"),
        ):
            await model_manager._download_model_task(DownloadParams(model_id="org/model"))

        assert captured_cmd[0] == sys.executable
        assert captured_cmd[1] == "-m"

        # The spawned module must be importable and expose a runnable __main__.
        spawned_module = captured_cmd[2]
        spec = importlib.util.find_spec(spawned_module)
        assert spec is not None, f"spawned module '{spawned_module}' is not importable"

        assert captured_cmd[3] == "download"
        assert "org/model" in captured_cmd
