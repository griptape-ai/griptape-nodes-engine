"""Test oversized blob-artifact values are blanked on the engine→UI emit path."""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.node_library.library_registry import LibraryRegistry
from griptape_nodes.retained_mode.events.base_events import EventResult, ExecutionEvent
from griptape_nodes.retained_mode.events.event_converter import safe_unstructure
from griptape_nodes.retained_mode.events.execution_events import (
    NodeResolvedEvent,
    ParameterValueUpdateEvent,
    StartFlowRequest,
    StartFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    CreateNodeResultSuccess,
    GetAllNodeInfoRequest,
    GetAllNodeInfoResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import (
    ClearAllObjectStateRequest,
    ClearAllObjectStateResultSuccess,
)
from griptape_nodes.retained_mode.events.parameter_events import (
    AlterElementEvent,
    GetParameterValueRequest,
    GetParameterValueResultSuccess,
    SetParameterValueRequest,
    SetParameterValueResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.settings import (
    DEFAULT_MAX_BLOB_ARTIFACT_B64_BYTES,
    MAX_BLOB_ARTIFACT_B64_BYTES_KEY,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "media_bloat_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "media_bloat_nodes.py"
LIBRARY_NAME = "Media Bloat Library"

# Raw payload sizes chosen so their base64 length (~1.33x raw) brackets the default
# threshold: `small` lands ~0.8x the default (under), `large` ~1.6x (over).
DEFAULT = DEFAULT_MAX_BLOB_ARTIFACT_B64_BYTES
SMALL_RAW = int(DEFAULT * 0.6)
LARGE_RAW = int(DEFAULT * 1.2)


pytestmark = [
    pytest.mark.skipif(
        not FIXTURE_LIBRARY_JSON_TEMPLATE.exists(),
        reason=f"Media Bloat Library fixture missing at {FIXTURE_LIBRARY_JSON_TEMPLATE}",
    ),
    pytest.mark.asyncio,
    pytest.mark.usefixtures("_media_bloat_library", "_clean_flow_state"),
]


async def test_no_oversized_blob_escapes_any_carrier() -> None:
    """Carrier-agnostic invariant: no over-threshold blob escapes on ANY event or result.

    Exercises the full lifecycle a real editor session hits — run (NodeResolvedEvent,
    ParameterValueUpdateEvent, AlterElementEvent), set/echo the stored output back
    (SetParameterValueResultSuccess, as workflow-load does), and hydrate the canvas
    (GetAllNodeInfo) — then asserts nothing over the threshold reached the wire.

    This is the primary RED gate and the guard against forgotten carriers: it fails until
    every value-bearing carrier is gated, and will fail again if a new one is ever added.
    """
    flow_name = _create_flow("bloat_backstop_wf")
    _create_node("InlineBytesImageNode", "ImageLarge", flow_name)
    _set_payload_size("ImageLarge", LARGE_RAW)

    with _capture_emitted() as captured:
        await _run_node(flow_name, "ImageLarge")

        # Full-resolution fetch (the escape hatch) — used only to obtain the stored value to set back.
        full = GriptapeNodes.handle_request(
            GetParameterValueRequest(node_name="ImageLarge", parameter_name="output_image")
        )
        assert isinstance(full, GetParameterValueResultSuccess), full

        # Set the stored output value back, as loading a saved workflow does — echoes finalized_value.
        set_result = GriptapeNodes.handle_request(
            SetParameterValueRequest(
                node_name="ImageLarge", parameter_name="output_image", value=full.value, is_output=True
            )
        )
        assert isinstance(set_result, SetParameterValueResultSuccess), set_result

        # Hydrate the canvas, as opening the workflow does.
        GriptapeNodes.handle_request(GetAllNodeInfoRequest(node_name="ImageLarge"))

    _assert_no_oversized_emitted(captured, DEFAULT)


async def test_default_threshold_blanks_large_preserves_small() -> None:
    """At the default threshold: an over-threshold image is blanked, an under-threshold one preserved.

    Asserted across the live-run event carriers (NodeResolvedEvent, ParameterValueUpdateEvent,
    AlterElementEvent) and canvas hydration (GetAllNodeInfo).
    """
    flow_name = _create_flow("bloat_default_wf")
    _create_node("InlineBytesImageNode", "ImageSmall", flow_name)
    _create_node("InlineBytesImageNode", "ImageLarge", flow_name)
    _set_payload_size("ImageSmall", SMALL_RAW)
    _set_payload_size("ImageLarge", LARGE_RAW)

    with _capture_emitted() as captured:
        await _run_node(flow_name, "ImageSmall")
        await _run_node(flow_name, "ImageLarge")
        GriptapeNodes.handle_request(GetAllNodeInfoRequest(node_name="ImageSmall"))
        GriptapeNodes.handle_request(GetAllNodeInfoRequest(node_name="ImageLarge"))

    small_resolved = _latest_event(captured, NodeResolvedEvent, node_name="ImageSmall")
    small_update = _latest_event(
        captured, ParameterValueUpdateEvent, node_name="ImageSmall", parameter_name="output_image"
    )
    small_alter = _latest_event(captured, AlterElementEvent, node_name="ImageSmall")
    _assert_preserved(_blob_value(small_resolved, "ImageArtifact"), "small NodeResolvedEvent")
    _assert_preserved(_blob_value(small_update, "ImageArtifact"), "small ParameterValueUpdateEvent")
    _assert_preserved(_blob_value(small_alter, "ImageArtifact"), "small AlterElementEvent")
    _assert_preserved(_node_info_value(captured, "ImageSmall", "ImageArtifact"), "small GetAllNodeInfo")

    large_resolved = _latest_event(captured, NodeResolvedEvent, node_name="ImageLarge")
    large_update = _latest_event(
        captured, ParameterValueUpdateEvent, node_name="ImageLarge", parameter_name="output_image"
    )
    large_alter = _latest_event(captured, AlterElementEvent, node_name="ImageLarge")
    _assert_blanked(_blob_value(large_resolved, "ImageArtifact"), "large NodeResolvedEvent")
    _assert_blanked(_blob_value(large_update, "ImageArtifact"), "large ParameterValueUpdateEvent")
    _assert_blanked(_blob_value(large_alter, "ImageArtifact"), "large AlterElementEvent")
    _assert_blanked(_node_info_value(captured, "ImageLarge", "ImageArtifact"), "large GetAllNodeInfo")


async def test_low_threshold_blanks_both() -> None:
    """A threshold below both payloads blanks both images across the run carriers and GetAllNodeInfo."""
    flow_name = _create_flow("bloat_low_wf")
    _create_node("InlineBytesImageNode", "ImageSmall", flow_name)
    _create_node("InlineBytesImageNode", "ImageLarge", flow_name)
    _set_payload_size("ImageSmall", SMALL_RAW)
    _set_payload_size("ImageLarge", LARGE_RAW)

    # Stripping reads the threshold at serialization time (.dict()), and the wire helpers call .dict()
    # in the assertions -- so the assertions must run while the override is still active.
    with _threshold_override(int(DEFAULT * 0.5)):
        with _capture_emitted() as captured:
            await _run_node(flow_name, "ImageSmall")
            await _run_node(flow_name, "ImageLarge")
            GriptapeNodes.handle_request(GetAllNodeInfoRequest(node_name="ImageSmall"))
            GriptapeNodes.handle_request(GetAllNodeInfoRequest(node_name="ImageLarge"))

        _assert_blanked(
            _blob_value(_latest_event(captured, AlterElementEvent, node_name="ImageSmall"), "ImageArtifact"),
            "small AlterElementEvent",
        )
        _assert_blanked(_node_info_value(captured, "ImageSmall", "ImageArtifact"), "small GetAllNodeInfo")
        _assert_blanked(
            _blob_value(_latest_event(captured, AlterElementEvent, node_name="ImageLarge"), "ImageArtifact"),
            "large AlterElementEvent",
        )
        _assert_blanked(_node_info_value(captured, "ImageLarge", "ImageArtifact"), "large GetAllNodeInfo")


async def test_high_threshold_preserves_both() -> None:
    """A threshold above both payloads preserves both images across the run carriers and GetAllNodeInfo."""
    flow_name = _create_flow("bloat_high_wf")
    _create_node("InlineBytesImageNode", "ImageSmall", flow_name)
    _create_node("InlineBytesImageNode", "ImageLarge", flow_name)
    _set_payload_size("ImageSmall", SMALL_RAW)
    _set_payload_size("ImageLarge", LARGE_RAW)

    # The wire helpers call .dict() (where stripping reads the threshold) in the assertions, so keep
    # them inside the override.
    with _threshold_override(DEFAULT * 2):
        with _capture_emitted() as captured:
            await _run_node(flow_name, "ImageSmall")
            await _run_node(flow_name, "ImageLarge")
            GriptapeNodes.handle_request(GetAllNodeInfoRequest(node_name="ImageSmall"))
            GriptapeNodes.handle_request(GetAllNodeInfoRequest(node_name="ImageLarge"))

        _assert_preserved(
            _blob_value(_latest_event(captured, AlterElementEvent, node_name="ImageSmall"), "ImageArtifact"),
            "small AlterElementEvent",
        )
        _assert_preserved(_node_info_value(captured, "ImageSmall", "ImageArtifact"), "small GetAllNodeInfo")
        _assert_preserved(
            _blob_value(_latest_event(captured, AlterElementEvent, node_name="ImageLarge"), "ImageArtifact"),
            "large AlterElementEvent",
        )
        _assert_preserved(_node_info_value(captured, "ImageLarge", "ImageArtifact"), "large GetAllNodeInfo")


async def test_get_parameter_value_returns_full_to_caller_but_blanked_on_wire() -> None:
    """GetParameterValue is NOT exempt: its broadcast copy is blanked like any other wire response.

    The object returned in-process keeps the full value (engine/library code that calls it directly
    relies on that), but the copy that goes over the wire is blanked -- a large response would
    overwhelm the transport regardless of which request produced it.
    """
    flow_name = _create_flow("bloat_get_param_wf")
    _create_node("InlineBytesImageNode", "ImageLarge", flow_name)
    _set_payload_size("ImageLarge", LARGE_RAW)
    await _run_node(flow_name, "ImageLarge")

    with _capture_emitted() as captured:
        result = GriptapeNodes.handle_request(
            GetParameterValueRequest(node_name="ImageLarge", parameter_name="output_image")
        )
    assert isinstance(result, GetParameterValueResultSuccess), result

    # The in-process return value keeps the full value...
    _assert_preserved(_blob_value(safe_unstructure(result), "ImageArtifact"), "returned GetParameterValue")
    # ...but the broadcast copy on the wire is blanked.
    broadcast = _latest_broadcast_result(
        captured, GetParameterValueResultSuccess, node_name="ImageLarge", parameter_name="output_image"
    )
    _assert_blanked(_blob_value(broadcast, "ImageArtifact"), "broadcast GetParameterValue")


async def test_audio_artifact_also_blanked() -> None:
    """The gate is type-agnostic: an over-threshold AudioArtifact is blanked too, not just images."""
    flow_name = _create_flow("bloat_audio_wf")
    _create_node("InlineBytesAudioNode", "AudioLarge", flow_name)
    _set_payload_size("AudioLarge", LARGE_RAW)

    with _capture_emitted() as captured:
        await _run_node(flow_name, "AudioLarge")
        GriptapeNodes.handle_request(GetAllNodeInfoRequest(node_name="AudioLarge"))

    _assert_blanked(
        _blob_value(_latest_event(captured, AlterElementEvent, node_name="AudioLarge"), "AudioArtifact"),
        "audio AlterElementEvent",
    )
    _assert_blanked(_node_info_value(captured, "AudioLarge", "AudioArtifact"), "audio GetAllNodeInfo")


def _materialize_library(target_dir: Path) -> Path:
    from griptape_nodes.utils.version_utils import engine_version

    target_dir.mkdir(parents=True, exist_ok=True)
    schema = json.loads(FIXTURE_LIBRARY_JSON_TEMPLATE.read_text())
    schema["metadata"]["engine_version"] = engine_version
    library_json = target_dir / "griptape_nodes_library.json"
    library_json.write_text(json.dumps(schema, indent=2))
    (target_dir / FIXTURE_NODE_FILE.name).write_text(FIXTURE_NODE_FILE.read_text())
    return library_json


def _create_node(node_type: str, node_name: str, flow_name: str) -> str:
    result = GriptapeNodes.handle_request(
        CreateNodeRequest(
            node_type=node_type,
            specific_library_name=LIBRARY_NAME,
            node_name=node_name,
            override_parent_flow_name=flow_name,
        )
    )
    assert isinstance(result, CreateNodeResultSuccess), result
    return result.node_name


def _set_payload_size(node_name: str, size_bytes: int) -> None:
    result = GriptapeNodes.handle_request(
        SetParameterValueRequest(parameter_name="payload_size", node_name=node_name, value=size_bytes)
    )
    assert isinstance(result, SetParameterValueResultSuccess), result


async def _run_node(flow_name: str, node_name: str) -> None:
    result = await GriptapeNodes.ahandle_request(
        StartFlowRequest(
            flow_name=flow_name,
            flow_node_name=node_name,
            wait_for_completion=True,
            completion_timeout_ms=30_000,
        )
    )
    assert isinstance(result, StartFlowResultSuccess), result


@pytest.fixture(scope="module")
def _media_bloat_library(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Register the fixture library once for the module (ClearAllObjectState leaves libraries intact)."""
    library_json = _materialize_library(tmp_path_factory.mktemp("media_bloat_library"))
    GriptapeNodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    register_result = GriptapeNodes.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result
    try:
        yield
    finally:
        GriptapeNodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        with contextlib.suppress(KeyError):
            LibraryRegistry.unregister_library(LIBRARY_NAME)


@pytest.fixture
def _clean_flow_state() -> Iterator[None]:
    """Clear flows/nodes before and after each test, leaving the library registered."""
    GriptapeNodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    try:
        yield
    finally:
        clear_result = GriptapeNodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        assert isinstance(clear_result, ClearAllObjectStateResultSuccess), clear_result


def _create_flow(workflow_name: str) -> str:
    GriptapeNodes.ContextManager().push_workflow(workflow_name=workflow_name)
    flow_result = GriptapeNodes.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="TestFlow", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    return flow_result.flow_name


@dataclass
class _Captured:
    """The wrapped events the engine put on the broadcast queue during a scenario.

    We keep the wrapped ExecutionEvent / EventResult objects and serialize them via their real
    ``.dict()`` -- which is where blob stripping now happens -- to inspect exactly what goes over the
    wire. We do NOT inspect the objects returned from handle_request: those keep their full values,
    because stripping runs only on the disposable serialized dict.
    """

    execution_events: list[ExecutionEvent] = field(default_factory=list)
    result_events: list[EventResult] = field(default_factory=list)


@contextlib.contextmanager
def _capture_emitted() -> Iterator[_Captured]:
    """Capture everything the engine broadcasts by draining the EventManager queue.

    put_event/aput_event early-return (skipping dispatch) when the queue is None, so we install a
    fresh queue for the duration and drain it afterwards. Any request whose result should be
    inspected must be issued INSIDE this block so its broadcast lands on the queue.
    """
    event_manager = GriptapeNodes.EventManager()
    # Snapshot/restore the queue so we don't leave a live queue installed on the singleton, which
    # would defeat the None-queue broadcast suppression other tests rely on. There's no public
    # restore: initialize_queue(None) *creates* a fresh queue rather than clearing it.
    previous_queue = event_manager._event_queue
    queue: asyncio.Queue = asyncio.Queue()
    event_manager.initialize_queue(queue)
    captured = _Captured()
    try:
        yield captured
    finally:
        event_manager._event_queue = previous_queue
        while not queue.empty():
            wrapped = getattr(queue.get_nowait(), "wrapped_event", None)
            if isinstance(wrapped, ExecutionEvent):
                captured.execution_events.append(wrapped)
            elif isinstance(wrapped, EventResult):
                captured.result_events.append(wrapped)


def _wire_payload(execution_event: ExecutionEvent) -> Any:
    """The serialized payload as it goes over the wire -- blob stripping runs inside ExecutionEvent.dict()."""
    return execution_event.dict()["payload"]


def _wire_result(result_event: EventResult) -> Any:
    """The serialized result as it goes over the wire -- blob stripping runs inside EventResult.dict()."""
    return result_event.dict()["result"]


@contextlib.contextmanager
def _threshold_override(value: int) -> Iterator[None]:
    """Temporarily set max_blob_artifact_b64_bytes, restoring the previous value afterward."""
    config_manager = GriptapeNodes.ConfigManager()
    original = config_manager.get_config_value(MAX_BLOB_ARTIFACT_B64_BYTES_KEY, default=DEFAULT)
    config_manager.set_config_value(MAX_BLOB_ARTIFACT_B64_BYTES_KEY, value)
    try:
        yield
    finally:
        config_manager.set_config_value(MAX_BLOB_ARTIFACT_B64_BYTES_KEY, original)


def _wire_frame_size(event: Any) -> int:
    """Byte length of the full serialized wire frame for an event -- what the transport actually sends."""
    return len(json.dumps(event.dict(), default=str))


def _wire_label(event: Any) -> str:
    """A short label identifying an event by its inner payload/result type, for failure messages."""
    inner = getattr(event, "result", None)
    if inner is None:
        inner = getattr(event, "payload", None)
    return f"{type(event).__name__}[{type(inner).__name__}]" if inner is not None else type(event).__name__


def _assert_no_oversized_emitted(captured: _Captured, max_frame_bytes: int) -> None:
    """The invariant: no emitted event serializes to a wire frame larger than the threshold.

    Measures the size of the *entire* serialized event (the real ``.dict()`` -> JSON, exactly what the
    transport sends), not specific artifact fields -- so it catches any oversized content regardless
    of where or how it is nested, and can't be fooled by a blob the field walker doesn't recognize.
    A frame whose blobs were all stripped is a few KB; an unstripped oversized blob pushes the frame
    past the threshold.
    """
    offenders = [
        f"{_wire_label(event)}: {size} bytes > {max_frame_bytes}"
        for event in (*captured.execution_events, *captured.result_events)
        if (size := _wire_frame_size(event)) > max_frame_bytes
    ]
    assert not offenders, "oversized wire frame(s) escaped the engine:\n  " + "\n  ".join(offenders)


def _event_node_name(payload: Any) -> Any:
    """The owning node name for an event, whether a top-level attr or nested in element_details."""
    name = getattr(payload, "node_name", None)
    if name is not None:
        return name
    details = getattr(payload, "element_details", None)
    if isinstance(details, dict):
        return details.get("node_name")
    return None


def _latest_event(captured: _Captured, event_type: type, **match: Any) -> Any:
    """Return the wire-serialized payload of the most recent captured execution event matching attrs.

    ``node_name`` is matched via ``_event_node_name`` so it works for AlterElementEvent (which
    carries the node name inside ``element_details``); other keys match top-level attributes.
    """
    wanted_node = match.pop("node_name", None)
    for execution_event in reversed(captured.execution_events):
        payload = execution_event.payload
        if not isinstance(payload, event_type):
            continue
        if wanted_node is not None and _event_node_name(payload) != wanted_node:
            continue
        if all(getattr(payload, k, None) == v for k, v in match.items()):
            return _wire_payload(execution_event)
    msg = f"No {event_type.__name__} captured matching node_name={wanted_node}, {match}"
    raise AssertionError(msg)


def _latest_broadcast_result(captured: _Captured, result_type: type, **request_match: Any) -> Any:
    """Return the wire-serialized result of the most recent broadcast result event of a type.

    Matches on the originating request's attributes (e.g. node_name), so the caller must have
    issued the request INSIDE the _capture_emitted block.
    """
    for result_event in reversed(captured.result_events):
        if not isinstance(result_event.result, result_type):
            continue
        request = result_event.request
        if all(getattr(request, key, None) == value for key, value in request_match.items()):
            return _wire_result(result_event)
    msg = f"No {result_type.__name__} broadcast captured matching {request_match}"
    raise AssertionError(msg)


def _node_info_value(captured: _Captured, node_name: str, artifact_type: str) -> Any:
    """The blob value (or None if blanked) the broadcast GetAllNodeInfo carried for a node's artifact.

    GetAllNodeInfoRequest must have been issued inside the _capture_emitted block.
    """
    serialized = _latest_broadcast_result(captured, GetAllNodeInfoResultSuccess, node_name=node_name)
    return _blob_value(serialized, artifact_type)


_MISSING = object()


def _search_artifact_value(serialized: Any, artifact_type: str) -> Any:
    """Return the first blob-artifact *leaf* value of the given type, or _MISSING.

    Matches an artifact dict only when its ``value`` is a leaf (base64 str, or None when
    blanked) — not a nested dict/list. This avoids colliding with payloads that carry their
    own ``type`` field (e.g. GetParameterValueResultSuccess.type == "ImageArtifact", whose
    ``value`` holds the artifact dict). Returns None (not _MISSING) when the artifact is
    present but blanked, so callers can distinguish "blanked" from "absent".
    """
    if isinstance(serialized, dict):
        value = serialized.get("value")
        if serialized.get("type") == artifact_type and not isinstance(value, (dict, list)):
            return value
        for child in serialized.values():
            found = _search_artifact_value(child, artifact_type)
            if found is not _MISSING:
                return found
    elif isinstance(serialized, list):
        for child in serialized:
            found = _search_artifact_value(child, artifact_type)
            if found is not _MISSING:
                return found
    return _MISSING


def _blob_value(serialized: Any, artifact_type: str) -> Any:
    """The base64 value (or None if blanked) of the first blob artifact of the given type; raises if absent."""
    found = _search_artifact_value(serialized, artifact_type)
    if found is _MISSING:
        msg = f"no {artifact_type} artifact found in serialized structure"
        raise AssertionError(msg)
    return found


def _assert_blanked(value: Any, where: str) -> None:
    assert value is None, f"{where}: value not blanked (len={len(value) if isinstance(value, str) else value!r})"


def _assert_preserved(value: Any, where: str) -> None:
    assert isinstance(value, str), f"{where}: value not preserved (got {type(value)})"
    assert value, f"{where}: value unexpectedly empty"
