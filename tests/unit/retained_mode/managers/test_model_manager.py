"""Tests for ModelManager methods added for model size support.

Covers:
- `on_handle_get_model_info_request` — token guard and HF API delegation
- `on_handle_search_models_request` — search result handling
- `on_handle_declare_model_invocation_request` — clears a declared invocation past the pre-dispatch chain
- `_download_model_task` — the spawned subprocess targets a runnable module
- `_load_status_file` — status file reads survive a concurrent status file write
"""

import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import portalocker
import pytest

from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete
from griptape_nodes.retained_mode.events.base_events import RequestPayload
from griptape_nodes.retained_mode.events.model_events import (
    DeclareModelInvocationRequest,
    DeclareModelInvocationResultFailure,
    DeclareModelInvocationResultSuccess,
    GetModelInfoRequest,
    GetModelInfoResultFailure,
    GetModelInfoResultSuccess,
    ListModelDownloadsRequest,
    ListModelDownloadsResultSuccess,
    SearchModelsRequest,
    SearchModelsResultFailure,
    SearchModelsResultSuccess,
)
from griptape_nodes.retained_mode.managers.event_manager import EventManager
from griptape_nodes.retained_mode.managers.model_manager import DownloadParams, ModelManager, _load_status_file


@pytest.fixture
def model_manager() -> ModelManager:
    """Bare ModelManager without event wiring."""
    return ModelManager()


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

    def test_authorization_checkpoint_denial_blocks_invocation(self, engine: Engine) -> None:
        # The InvokeModel checkpoint gates the declared invocation: a denial from
        # a registered authorization hook turns into a failure so the node does
        # not invoke the model. The handler passes the stable catalog key; the app
        # resolves the provider and family from it.
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

        engine.event_manager.add_authorization_hook(deny)
        manager = ModelManager()

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

    def test_empty_failure_denial_still_yields_a_reason(self, engine: Engine) -> None:
        # A hook that misuses the contract by returning a denial with no failures
        # (it should return None to allow) must not produce a reason-less message.
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
            AuthorizationCheckpoint,
            CheckpointDenial,
        )

        def deny(_checkpoint: AuthorizationCheckpoint) -> CheckpointDenial:
            return CheckpointDenial(failures=())

        engine.event_manager.add_authorization_hook(deny)
        manager = ModelManager()

        denied = manager.on_handle_declare_model_invocation_request(
            DeclareModelInvocationRequest(model_id="gtc_claude_opus_4_7")
        )
        assert isinstance(denied, DeclareModelInvocationResultFailure)
        assert "Denied by the license policy." in str(denied.result_details)


_CATALOG_LIBRARY_NAME = "model-manager-invocation-test-library"


class _ProbeModelNode(BaseNode):
    """Concrete node whose declared catalog models drive invocation enrichment."""

    def __init__(self, name: str, metadata=None) -> None:  # noqa: ANN001
        super().__init__(name=name, metadata=metadata)


class TestDeclareModelInvocationCatalogEnrichment:
    """`_model_checkpoint_attributes` enriches the InvokeModel checkpoint.

    The declared stable catalog key resolves against the declaring node's catalog
    models into the same `provider_id` / `model_families` facts the OfferModel
    (dropdown) and InstantiateNode checkpoints carry, so a provider- or
    family-scoped policy gates an invocation the way it gates the picker.
    """

    @pytest.fixture(autouse=True)
    def _clean_registry(self):  # noqa: ANN202
        from griptape_nodes.node_library.library_registry import LibraryRegistry

        LibraryRegistry._clear()
        yield
        LibraryRegistry._clear()

    @staticmethod
    def _catalog():  # noqa: ANN205
        from griptape_nodes.node_library.library_declarations import (
            KeySupport,
            Model,
            ModelCatalogLibraryProperty,
            ModelProvider,
        )

        return ModelCatalogLibraryProperty(
            providers={
                "openai": ModelProvider(
                    display_name="OpenAI",
                    models={
                        "gtc_gpt_image_1_mini": Model(
                            display_name="GPT Image 1 Mini",
                            family="GPT Image",
                            provider_model_id="gpt-image-1-mini",
                            key_support=KeySupport.SUPPORTS_CUSTOMER_KEY_OR_GRIPTAPE_KEY,
                        ),
                    },
                )
            }
        )

    def _register_node(self, node_name: str, engine: Engine) -> None:
        """Register a library + probe node type, then a node instance the handler can resolve."""
        from griptape_nodes.node_library.library_declarations import ModelUsageNodeProperty
        from griptape_nodes.node_library.library_registry import (
            LibraryMetadata,
            LibraryRegistry,
            LibrarySchema,
            NodeMetadata,
        )

        schema = LibrarySchema(
            name=_CATALOG_LIBRARY_NAME,
            library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
            metadata=LibraryMetadata(
                author="t",
                description="d",
                library_version="1.0.0",
                engine_version="1.0.0",
                tags=[],
                declarations=[self._catalog()],
            ),
            categories=[],
            nodes=[],
        )
        library = LibraryRegistry.generate_new_library(library_data=schema)
        library.register_new_node_type(
            _ProbeModelNode,
            NodeMetadata(
                category="t",
                description="d",
                display_name="Probe",
                declarations=[ModelUsageNodeProperty(model_ids=["gtc_gpt_image_1_mini"])],
            ),
        )
        node = _ProbeModelNode(
            name=node_name,
            metadata={"library": _CATALOG_LIBRARY_NAME, "node_type": _ProbeModelNode.__name__},
        )
        engine.object_manager.add_object_by_name(node_name, node)

    def test_checkpoint_carries_family_and_provider_for_declared_model(
        self,
        engine: Engine,
    ) -> None:
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
            AuthorizationCheckpoint,
            CheckpointDenial,
        )

        self._register_node("Probe_1", engine)
        seen: dict[str, object] = {}

        def capture(checkpoint: AuthorizationCheckpoint) -> CheckpointDenial | None:
            seen["attributes"] = dict(checkpoint.attributes)
            return None

        engine.event_manager.add_authorization_hook(capture)
        manager = ModelManager()

        result = manager.on_handle_declare_model_invocation_request(
            DeclareModelInvocationRequest(model_id="gtc_gpt_image_1_mini", node_name="Probe_1")
        )

        assert isinstance(result, DeclareModelInvocationResultSuccess)
        # The InvokeModel checkpoint now carries the provider and family the
        # declared catalog key resolves to, not just the bare id.
        assert seen["attributes"] == {
            "id": "gtc_gpt_image_1_mini",
            "provider_id": "openai",
            "model_families": ["GPT Image"],
        }

    def test_family_scoped_hook_blocks_the_invocation(self, engine: Engine) -> None:
        # Mirrors a license policy that forbids a family via the attribute form
        # `resource.model_families.contains(...)`: with the family now on the
        # InvokeModel checkpoint, that forbid fires at invocation time, not only
        # on the dropdown query.
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
            AuthorizationCheckpoint,
            CheckpointDenial,
            CheckpointFailure,
        )

        self._register_node("Probe_1", engine)

        def deny(checkpoint: AuthorizationCheckpoint) -> CheckpointDenial | None:
            if "GPT Image" in (checkpoint.attributes.get("model_families") or []):
                return CheckpointDenial(failures=(CheckpointFailure(detail="GPT Image family is not in your plan."),))
            return None

        engine.event_manager.add_authorization_hook(deny)
        manager = ModelManager()

        result = manager.on_handle_declare_model_invocation_request(
            DeclareModelInvocationRequest(model_id="gtc_gpt_image_1_mini", node_name="Probe_1")
        )

        assert isinstance(result, DeclareModelInvocationResultFailure)
        assert "GPT Image family is not in your plan." in str(result.result_details)

    def test_key_absent_from_node_models_falls_back_to_bare_id(self, engine: Engine) -> None:
        # A key the node does not declare cannot be enriched; the checkpoint
        # carries only the bare id, so a family/provider rule cannot match but a
        # bare-id rule still can.
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
            AuthorizationCheckpoint,
            CheckpointDenial,
        )

        self._register_node("Probe_1", engine)
        seen: dict[str, object] = {}

        def capture(checkpoint: AuthorizationCheckpoint) -> CheckpointDenial | None:
            seen["attributes"] = dict(checkpoint.attributes)
            return None

        engine.event_manager.add_authorization_hook(capture)
        manager = ModelManager()

        manager.on_handle_declare_model_invocation_request(
            DeclareModelInvocationRequest(model_id="gtc_not_declared", node_name="Probe_1")
        )

        assert seen["attributes"] == {"id": "gtc_not_declared"}

    def test_missing_node_name_falls_back_to_bare_id(self, engine: Engine) -> None:
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
            AuthorizationCheckpoint,
            CheckpointDenial,
        )

        seen: dict[str, object] = {}

        def capture(checkpoint: AuthorizationCheckpoint) -> CheckpointDenial | None:
            seen["attributes"] = dict(checkpoint.attributes)
            return None

        engine.event_manager.add_authorization_hook(capture)
        manager = ModelManager()

        manager.on_handle_declare_model_invocation_request(
            DeclareModelInvocationRequest(model_id="gtc_gpt_image_1_mini")
        )

        # No node to resolve against -- only the bare id, as before.
        assert seen["attributes"] == {"id": "gtc_gpt_image_1_mini"}


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


# ---------------------------------------------------------------------------
# _load_status_file — reads that land on a status file being written
# ---------------------------------------------------------------------------


_COMPLETED_RECORD = {
    "model_id": "depth-anything/DA3-SMALL",
    "status": "completed",
    "started_at": "2026-09-03T11:12:30+00:00",
    "updated_at": "2026-09-03T11:13:02+00:00",
    "completed_at": "2026-09-03T11:13:02+00:00",
    "total_bytes": 100,
    "downloaded_bytes": 100,
    "progress_percent": 100.0,
}


class TestStatusFileReadsSurviveConcurrentWrites:
    """A status file mid-write must not turn a poll into a reported failure.

    The collision `TestAppInitializationCompleteWorkerGuard` describes
    (griptape-ai/griptape-nodes-engine#5373), reached through the reader that guard could not
    remove: the terminal `"completed"` write holds the lock while the editor polls for
    progress, so the handler reported `PermissionError: [Errno 13] Permission denied` on the
    line after "Successfully downloaded model".
    """

    @pytest.fixture
    def status_file(self, tmp_path: Path) -> Path:
        path = tmp_path / "depth-anything--DA3-SMALL.json"
        path.write_text(json.dumps(_COMPLETED_RECORD), encoding="utf-8")
        return path

    def test_retries_past_a_locked_file(self, status_file: Path) -> None:
        real_open = Path.open
        attempts = []

        def open_locked_once(self_path: Path, *args: Any, **kwargs: Any) -> Any:
            attempts.append(self_path)
            if len(attempts) == 1:
                raise PermissionError(13, "Permission denied")
            return real_open(self_path, *args, **kwargs)

        with patch.object(Path, "open", open_locked_once):
            data = _load_status_file(status_file)

        # The failed read plus the retry that got the value.
        assert attempts == [status_file, status_file]
        assert data == _COMPLETED_RECORD

    def test_retries_past_a_half_written_file(self, status_file: Path) -> None:
        # The write truncates before it locks, so a reader can see zero bytes.
        real_open = Path.open
        attempts = []

        def open_truncated_once(self_path: Path, *args: Any, **kwargs: Any) -> Any:
            attempts.append(self_path)
            if len(attempts) == 1:
                return io.StringIO("")
            return real_open(self_path, *args, **kwargs)

        with patch.object(Path, "open", open_truncated_once):
            data = _load_status_file(status_file)

        # The failed read plus the retry that got the value.
        assert attempts == [status_file, status_file]
        assert data == _COMPLETED_RECORD

    def test_a_file_that_never_frees_up_is_dropped_not_raised(self, status_file: Path) -> None:
        with patch.object(Path, "open", side_effect=PermissionError(13, "Permission denied")):
            assert _load_status_file(status_file) is None

    def test_missing_file_reads_as_no_status(self, tmp_path: Path) -> None:
        assert _load_status_file(tmp_path / "never-downloaded.json") is None

    @pytest.mark.skipif(sys.platform != "win32", reason="only Windows fails a read inside another handle's lock")
    def test_a_real_exclusive_lock_is_contained(self, status_file: Path) -> None:
        # The lock os_manager takes for every status file write.
        with portalocker.Lock(
            str(status_file),
            mode="r+",
            timeout=0,
            flags=portalocker.LockFlags.EXCLUSIVE | portalocker.LockFlags.NON_BLOCKING,
        ):
            assert _load_status_file(status_file) is None

    @pytest.mark.asyncio
    async def test_list_downloads_still_succeeds_when_a_read_fails(
        self, model_manager: ModelManager, status_file: Path
    ) -> None:
        with (
            patch.object(model_manager, "_get_status_directory", return_value=status_file.parent),
            patch.object(Path, "open", side_effect=PermissionError(13, "Permission denied")),
        ):
            result = await model_manager.on_handle_list_model_downloads_request(ListModelDownloadsRequest())

        # An unreadable record drops out of the list; it does not fail the request.
        assert isinstance(result, ListModelDownloadsResultSuccess)
        assert result.downloads == []

    @pytest.mark.asyncio
    async def test_list_downloads_reads_a_settled_file(self, model_manager: ModelManager, status_file: Path) -> None:
        with patch.object(model_manager, "_get_status_directory", return_value=status_file.parent):
            result = await model_manager.on_handle_list_model_downloads_request(ListModelDownloadsRequest())

        assert isinstance(result, ListModelDownloadsResultSuccess)
        assert [download.model_id for download in result.downloads] == ["depth-anything/DA3-SMALL"]
        assert result.downloads[0].status == "completed"


class TestAppInitializationCompleteWorkerGuard:
    """Startup model downloads belong to the orchestrator alone."""

    @pytest.mark.asyncio
    async def test_worker_skips_startup_downloads(self, model_manager: ModelManager) -> None:
        """A worker must not scan or resume downloads.

        Every worker shares the orchestrator's status directory. When workers resumed
        too, they read those files while the orchestrator was writing them, which on
        Windows surfaces as PermissionError from the writer's exclusive lock and took
        down the whole AppInitializationComplete broadcast.
        """
        with (
            patch.object(model_manager, "_find_unfinished_downloads") as find_unfinished,
            patch.object(model_manager, "on_handle_download_model_request") as handle_download,
        ):
            await model_manager.on_app_initialization_complete(AppInitializationComplete(is_worker=True))

        find_unfinished.assert_not_called()
        handle_download.assert_not_called()

    @pytest.mark.asyncio
    async def test_orchestrator_resumes_unfinished_downloads(self, model_manager: ModelManager) -> None:
        """The orchestrator still resumes, so the guard cannot be read as "never resume"."""
        engine = SimpleNamespace(config_manager=SimpleNamespace(get_config_value=lambda *_args, **_kwargs: []))

        with (
            patch.object(type(model_manager), "engine", property(lambda _self: engine)),
            patch.object(model_manager, "_find_unfinished_downloads", return_value=["org/model"]) as find_unfinished,
            patch.object(model_manager, "on_handle_download_model_request", new_callable=AsyncMock) as handle_download,
        ):
            await model_manager.on_app_initialization_complete(AppInitializationComplete(is_worker=False))

        find_unfinished.assert_called_once()
        assert handle_download.await_args_list[0].args[0].model_id == "org/model"
