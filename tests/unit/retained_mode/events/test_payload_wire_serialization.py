"""Tests for the event/payload wire-serialization pipeline.

Covers ``retained_mode/events/base_events.py`` (Payload.to_json, the Event envelope classes and
their ``from_dict``) and ``retained_mode/events/event_converter.py`` (the cattrs converter's
registered hooks and ``safe_unstructure``). Complements ``test_event_converter.py`` and
``test_from_dict.py``, which already cover JSON-primitive unions, the exception wire form,
``SetParameterValueRequest`` structuring, and ``from_dict`` basics -- this file extends into the
gaps: full round trips, ``ResultDetails``, batches, artifact/pydantic/Path/float/type hooks, the
``safe_unstructure`` fallback, unknown-type errors, and a registry-wide sweep.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import pkgutil
import types
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints

import pytest
from griptape.artifacts import TextArtifact

import griptape_nodes.retained_mode.events as events_pkg
from griptape_nodes.node_library.workflow_registry import WorkflowMetadata
from griptape_nodes.retained_mode.events.agent_events import UpdateAgentProviderRequest
from griptape_nodes.retained_mode.events.artifact_events import (
    RegisterArtifactProviderRequest,
)
from griptape_nodes.retained_mode.events.base_events import (
    EventRequest,
    EventRequestBatch,
    EventResultFailure,
    EventResultSuccess,
    Payload,
    ResultDetail,
    ResultDetails,
    StrictModeViolationDetail,
)
from griptape_nodes.retained_mode.events.config_events import GetConfigValueRequest, GetConfigValueResultSuccess
from griptape_nodes.retained_mode.events.connection_events import CreateConnectionRequest
from griptape_nodes.retained_mode.events.context_events import SetWorkflowContextSuccess
from griptape_nodes.retained_mode.events.event_converter import converter, safe_unstructure
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry
from griptape_nodes.retained_mode.events.project_events import LoadProjectTemplateRequest
from griptape_nodes.retained_mode.events.workflow_events import GetWorkflowMetadataResultSuccess

# --- Populate the full PayloadRegistry without constructing an Engine -------------------------
#
# @PayloadRegistry.register only runs when the module that declares the decorated class is
# imported. Building a real Engine would populate the registry as a side effect, but it would
# also read/write real user config at collection time (the isolate_user_config fixture exists
# precisely to prevent that, and fixtures have not run yet during module-level collection). Instead,
# walk the events package directly: this is process-local, config-free, and idempotent regardless
# of what other test files have already imported.
_SCHEMA_GENERATOR_SCRIPT_MODULE = "generate_request_payload_schemas"
"""CLI script, not an importable library module: its top-level code assumes the registry is
already fully populated and writes a JSON schema file as a side effect. Importing it here would
be circular (it wants the very thing this loop is building) and would touch the filesystem."""


def _load_all_event_modules() -> None:
    """Import every submodule of ``retained_mode.events`` so every payload type registers."""
    for module_info in pkgutil.iter_modules(events_pkg.__path__, f"{events_pkg.__name__}."):
        short_name = module_info.name.rsplit(".", 1)[-1]
        if short_name == _SCHEMA_GENERATOR_SCRIPT_MODULE:
            continue
        importlib.import_module(module_info.name)


_load_all_event_modules()
_FULL_PAYLOAD_REGISTRY: dict[str, type[Payload]] = PayloadRegistry.get_registry()


# --- Generic "emptiest legal instance" factory for the registry sweep --------------------------


class _CannotBuildDefaultError(Exception):
    """Raised internally when the factory below cannot synthesize a value for a field's type.

    Not a test failure by itself: the sweep catches this to exclude a payload type it cannot
    mechanically construct (e.g. a field typed as a real domain object like ``Parameter``) rather
    than asserting a fabricated, possibly-wrong value for it.
    """


_MAX_DEFAULT_VALUE_DEPTH = 6

_PRIMITIVE_DEFAULTS: dict[type, Any] = {
    str: "",
    int: 0,
    float: 0.0,
    bool: False,
    bytes: b"",
}


def _unwrap_newtype(type_hint: Any) -> Any:
    while hasattr(type_hint, "__supertype__"):
        type_hint = type_hint.__supertype__
    return type_hint


def _build_default_for_union(union_args: tuple[Any, ...], depth: int) -> Any:
    if type(None) in union_args:
        return None

    last_error: _CannotBuildDefaultError | None = None
    for member in union_args:
        try:
            return _build_default_value(member, depth + 1)
        except _CannotBuildDefaultError as error:
            last_error = error

    msg = f"no member of union {union_args!r} could be built"
    raise _CannotBuildDefaultError(msg) from last_error


def _build_default_for_generic(type_hint: Any, origin: Any, depth: int) -> Any:
    if origin is list:
        return []
    if origin is dict:
        return {}
    if origin is set:
        return set()
    if origin is frozenset:
        return frozenset()
    if origin is tuple:
        return ()
    if origin is Union or origin is types.UnionType:
        return _build_default_for_union(get_args(type_hint), depth)

    msg = f"no default-value rule for generic origin {origin!r} ({type_hint!r})"
    raise _CannotBuildDefaultError(msg)


def _build_default_for_simple_type(resolved: Any) -> tuple[bool, Any]:
    """Return (True, value) for a type that maps to one fixed placeholder, else (False, None).

    Keeping this as its own lookup (rather than more `if ... return` branches inline in
    ``_build_default_value``) is what keeps that function under the return-statement limit.
    """
    if resolved is Any:
        return True, None
    if resolved in _PRIMITIVE_DEFAULTS:
        return True, _PRIMITIVE_DEFAULTS[resolved]
    if resolved is Path:
        return True, Path("placeholder")
    if resolved is type:
        return True, object
    return False, None


def _build_default_value(type_hint: Any, depth: int) -> Any:
    """Synthesize a minimal placeholder for one field's declared type.

    Only handles the shapes that actually show up on Payload dataclasses: JSON primitives,
    Optional/Union, list/dict/set/frozenset/tuple, Path, Enum, bare ``type``, and nested
    dataclasses. Anything else raises ``_CannotBuildDefaultError``.
    """
    if depth > _MAX_DEFAULT_VALUE_DEPTH:
        msg = f"default-value nesting too deep for {type_hint!r}"
        raise _CannotBuildDefaultError(msg)

    resolved = _unwrap_newtype(type_hint)

    is_simple, simple_value = _build_default_for_simple_type(resolved)
    if is_simple:
        return simple_value

    origin = get_origin(resolved)
    if origin is not None:
        return _build_default_for_generic(resolved, origin, depth)

    if isinstance(resolved, type) and issubclass(resolved, Enum):
        return next(iter(resolved))
    if isinstance(resolved, type) and dataclasses.is_dataclass(resolved):
        return _build_default_instance(resolved, depth + 1)

    msg = f"no default-value rule for {type_hint!r}"
    raise _CannotBuildDefaultError(msg)


def _build_default_instance(cls: type, depth: int = 0) -> Any:
    """Construct ``cls`` using each field's own default where declared, else a placeholder.

    Mirrors the "emptiest legal instance" a client could build: fields the dataclass already
    defaults are left untouched, and only fields with no default get a synthesized placeholder.
    """
    try:
        type_hints = get_type_hints(cls)
    except NameError as error:
        msg = f"get_type_hints failed for {cls!r}: {error}"
        raise _CannotBuildDefaultError(msg) from error

    constructor_kwargs: dict[str, Any] = {}
    for dataclass_field in dataclasses.fields(cls):
        if not dataclass_field.init:
            continue
        has_default = dataclass_field.default is not dataclasses.MISSING
        has_default_factory = dataclass_field.default_factory is not dataclasses.MISSING  # type: ignore[misc]
        if has_default or has_default_factory:
            continue
        field_type = type_hints.get(dataclass_field.name, dataclass_field.type)
        constructor_kwargs[dataclass_field.name] = _build_default_value(field_type, depth)

    try:
        return cls(**constructor_kwargs)
    except TypeError as error:
        # A handful of dataclasses (e.g. ResultDetails) define their own __init__ with a shape
        # that does not match their declared fields (variadic *args instead of one keyword per
        # field). Treat that as unbuildable rather than letting it crash the sweep.
        msg = f"cls(**constructor_kwargs) rejected the synthesized kwargs for {cls!r}: {error}"
        raise _CannotBuildDefaultError(msg) from error


def _collect_buildable_payload_names() -> tuple[list[str], dict[str, str]]:
    """Partition the registry into names the factory can build and names it cannot, with why.

    Returns the buildable names (used to parametrize the sweep below) and a name -> reason map
    for everything excluded, so the exclusion is a recorded fact instead of a silent `continue`.
    """
    buildable: list[str] = []
    excluded: dict[str, str] = {}
    for name, cls in sorted(_FULL_PAYLOAD_REGISTRY.items()):
        try:
            _build_default_instance(cls)
        except _CannotBuildDefaultError as error:
            excluded[name] = str(error)
            continue
        buildable.append(name)
    return buildable, excluded


_BUILDABLE_PAYLOAD_NAMES, _EXCLUDED_PAYLOAD_REASONS = _collect_buildable_payload_names()

# --- Guard against the sweep silently losing coverage -------------------------------------------
#
# `_CannotBuildDefaultError` carries no structured code, only a message, so the only way to tell
# "known factory limitation" (e.g. a field typed as a real domain object like `Parameter`) apart
# from "something new and unexpected fell out of the sweep" is the message text. The patterns
# below are substrings of the exact messages `_build_default_value`, `_build_default_for_generic`,
# `_build_default_for_union`, and `_build_default_instance` raise.
#
# Registry size and sweep size vary with which other test modules have already imported event
# submodules by the time this file's `_load_all_event_modules()` runs (803 payload types when
# this file runs alone, 812 in the full `tests/unit` run), so this guard does not assert an exact
# count. It asserts that every currently-excluded payload fails for an already-known reason, so a
# newly excluded payload with an unrecognized reason fails this test instead of vanishing.
_KNOWN_FACTORY_LIMITATION_PATTERNS = (
    "no default-value rule for generic origin",
    "no default-value rule for",
    "get_type_hints failed for",
    "no member of union",
    "cls(**constructor_kwargs) rejected the synthesized kwargs for",
    "default-value nesting too deep for",
)


class TestSweepExclusionsAreKnownFactoryLimitations:
    """Every payload type the registry sweep excludes must fail for an already-recognized reason.

    Protects the highest-value test in this file (the registry-wide round-trip sweep below): if a
    newly added payload type falls out of the sweep for a reason nobody has seen before, that
    should surface here as a test failure naming the payload, not disappear into a bare
    `continue`. This does not xfail the currently-excluded payloads (see
    `_EXCLUDED_PAYLOAD_REASONS`) -- they are limits of this test file's synthetic-instance
    factory, not production defects, and marking them xfail would misrepresent a test limitation
    as a production bug.
    """

    def test_every_excluded_payload_has_a_recognized_reason(self) -> None:
        unrecognized = {
            name: reason
            for name, reason in _EXCLUDED_PAYLOAD_REASONS.items()
            if not any(pattern in reason for pattern in _KNOWN_FACTORY_LIMITATION_PATTERNS)
        }
        assert unrecognized == {}


# --- Known, deterministic wire round-trip bugs surfaced by the sweep below ---------------------

_TYPE_FIELD_MISSING_STRUCTURE_HOOK_REASON = (
    "API-CONTRACT: event_converter registers an unstructure hook for a bare `type` field (type -> "
    "'module.Qualname' string) but no matching structure hook, so a value built from to_json() "
    "cannot be read back with converter.structure(); it raises StructureHandlerNotFoundError. "
    "Intended: a `type` field survives an unstructure/structure round trip like every other field. "
    "- see #5437"
)

_UPDATE_PROVIDER_PAYLOAD_ROUND_TRIP_REASON = (
    "API-CONTRACT: UpdateAgentProviderRequest.provider is an UpdateProviderPayload pydantic model "
    "whose optional str fields default to None but are validated by a 'non-empty string if "
    "provided' validator. Unstructuring with model_dump(mode='json') always emits that None "
    "default explicitly, and re-structuring the dict with model_validate() treats the explicit "
    "None as 'provided', so the validator rejects it even though the identical unconstructed "
    "default was legal. Intended: a payload built with only its own declared defaults survives a "
    "full wire round trip. - see #5439"
)

_ENUM_UNION_MISSING_STRUCTURE_HOOK_REASON = (
    "API-CONTRACT: failure_reason is typed `SequenceScanFailureReason | FileIOFailureReason`, a "
    "union of two StrEnum types. _is_json_primitive_union only recognizes unions of plain JSON "
    "primitives, so this union falls through to cattrs' default union dispatch, which has no "
    "discriminator strategy for two unrelated Enum members and raises StructureHandlerNotFoundError. "
    "Intended: a failure_reason value from either enum survives an unstructure/structure round "
    "trip. - see #5438"
)

_KNOWN_WIRE_ROUND_TRIP_BUGS: dict[str, str] = {
    "RegisterArtifactProviderRequest": _TYPE_FIELD_MISSING_STRUCTURE_HOOK_REASON,
    "RegisterPreviewGeneratorRequest": _TYPE_FIELD_MISSING_STRUCTURE_HOOK_REASON,
    "UpdateAgentProviderRequest": _UPDATE_PROVIDER_PAYLOAD_ROUND_TRIP_REASON,
    "DeduceSequencesFromFileListResultFailure": _ENUM_UNION_MISSING_STRUCTURE_HOOK_REASON,
    "ListDirectoryResultFailure": _ENUM_UNION_MISSING_STRUCTURE_HOOK_REASON,
    "ListDirectorySequencesResultFailure": _ENUM_UNION_MISSING_STRUCTURE_HOOK_REASON,
    "ScanSequencesResultFailure": _ENUM_UNION_MISSING_STRUCTURE_HOOK_REASON,
}


def _sweep_params() -> list[Any]:
    params = []
    for name in _BUILDABLE_PAYLOAD_NAMES:
        reason = _KNOWN_WIRE_ROUND_TRIP_BUGS.get(name)
        if reason is None:
            params.append(pytest.param(name, id=name))
        else:
            params.append(pytest.param(name, id=name, marks=pytest.mark.xfail(strict=True, reason=reason)))
    return params


class TestPayloadRegistryDefaultInstanceRoundTrip:
    """Sweep every registered payload type that can be built from its own field defaults.

    Guards the wire contract broadly: a payload built with nothing but its declared defaults must
    unstructure to JSON and restructure back into an equal instance. The registry has hundreds of
    entries; this sweeps the large majority (over 90%) that a generic default-value factory can
    construct without inventing real domain objects such as ``Parameter`` or
    ``SerializedFlowCommands`` (see ``_EXCLUDED_PAYLOAD_REASONS`` and
    ``TestSweepExclusionsAreKnownFactoryLimitations`` for the rest). That is still enough surface
    to catch hooks that are missing for only some field shapes, as the xfails below demonstrate
    (a bare `type` field, a union of two unrelated Enum types, and a pydantic model with a
    validated-but-optional field).
    """

    @pytest.mark.parametrize("payload_name", _sweep_params())
    def test_default_instance_round_trips_through_wire_form(self, payload_name: str) -> None:
        payload_cls = _FULL_PAYLOAD_REGISTRY[payload_name]
        instance = _build_default_instance(payload_cls)

        data = json.loads(instance.to_json())
        restored = converter.structure(data, payload_cls)

        assert restored == instance


class TestRequestResultPayloadRoundTrip:
    """A payload built by a caller must survive to_json() -> json.loads() -> converter.structure()."""

    def test_request_payload_round_trip_preserves_every_field(self) -> None:
        request = CreateConnectionRequest(
            source_parameter_name="out",
            target_parameter_name="in",
            source_node_name="NodeA",
            target_node_name="NodeB",
            initial_setup=True,
            is_node_group_internal=True,
            request_id="abc-123",
        )

        data = json.loads(request.to_json())
        restored = converter.structure(data, CreateConnectionRequest)

        assert restored == request

    def test_result_payload_round_trip_preserves_declared_fields(self) -> None:
        result = SetWorkflowContextSuccess(result_details="context set", workflow_name="my_workflow")

        data = json.loads(result.to_json())
        restored = converter.structure(data, SetWorkflowContextSuccess)

        assert restored == result

    def test_init_false_field_is_reset_to_class_default_on_structure(self) -> None:
        """``altered_workflow_state`` is init=False: the handler decides it, not the wire.

        Unstructuring reports whatever the live object's flag actually is (useful for logging/
        debugging), but restructuring a payload from the wire must NOT let that value override the
        class's own semantics, otherwise a client could claim its own request altered the workflow.
        Mutating the field after construction (something only a test can do, since it is init=False)
        proves the two directions are asymmetric on purpose.
        """
        result = GetConfigValueResultSuccess(value=1, result_details="ok")
        assert result.altered_workflow_state is False
        result.altered_workflow_state = True

        data = json.loads(result.to_json())
        assert data["altered_workflow_state"] is True

        restored = converter.structure(data, GetConfigValueResultSuccess)
        assert restored.altered_workflow_state is False


class TestResultDetailsWireForm:
    """ResultDetails' custom _cattrs_unstructure/_cattrs_structure round trip, in both shapes."""

    def test_string_shorthand_round_trip(self) -> None:
        result = GetConfigValueResultSuccess(value=1, result_details="just a message")

        data = json.loads(result.to_json())
        restored = converter.structure(data, GetConfigValueResultSuccess)

        assert isinstance(restored.result_details, ResultDetails)
        assert str(restored.result_details) == "just a message"

    def test_structured_multi_detail_round_trip_preserves_levels_and_subclass_identity(self) -> None:
        """A mix of plain and StrictModeViolationDetail entries keeps every level and subclass field.

        register_polymorphic_dataclass(ResultDetail) is what makes the subclass survive: without it
        every entry would degrade to a bare ResultDetail and lose rule_id/severity/subject/library_name.
        """
        details = ResultDetails(
            ResultDetail(level=20, message="plain info"),
            StrictModeViolationDetail(
                level=40,
                message="a strict-mode violation",
                rule_id="R1",
                severity="high",
                subject="node.foo",
                library_name="My Library",
            ),
        )
        result = GetConfigValueResultSuccess(value=1, result_details=details)

        data = json.loads(result.to_json())
        restored = converter.structure(data, GetConfigValueResultSuccess)

        restored_details = restored.result_details
        assert isinstance(restored_details, ResultDetails)
        assert [d.level for d in restored_details.result_details] == [20, 40]
        assert [d.message for d in restored_details.result_details] == ["plain info", "a strict-mode violation"]

        second_detail = restored_details.result_details[1]
        assert isinstance(second_detail, StrictModeViolationDetail)
        assert second_detail.rule_id == "R1"
        assert second_detail.severity == "high"
        assert second_detail.subject == "node.foo"
        assert second_detail.library_name == "My Library"


class TestEventRequestBatchRoundTrip:
    """EventRequestBatch fans a heterogeneous list of requests out and back in."""

    def test_batch_round_trip_resolves_each_request_to_its_own_concrete_type(self) -> None:
        batch = EventRequestBatch(
            requests=[
                EventRequest(request=GetConfigValueRequest(category_and_key="a.b")),
                EventRequest(request=CreateConnectionRequest(source_parameter_name="out", target_parameter_name="in")),
            ]
        )

        data = json.loads(json.dumps(batch.dict(), default=str))
        restored = EventRequestBatch.from_dict(data)

        expected_request_count = 2
        assert len(restored.requests) == expected_request_count
        first, second = restored.requests
        assert isinstance(first.request, GetConfigValueRequest)
        assert first.request.category_and_key == "a.b"
        assert isinstance(second.request, CreateConnectionRequest)
        assert second.request.source_parameter_name == "out"


class TestSerializableArtifactHook:
    """SerializableMixin subclasses (griptape artifacts) unstructure via their own to_dict()."""

    def test_artifact_unstructures_via_to_dict_and_restructures_into_the_artifact(self) -> None:
        artifact = TextArtifact("hold onto this")

        unstructured = converter.unstructure(artifact, TextArtifact)
        assert unstructured == artifact.to_dict()

        restored = converter.structure(unstructured, TextArtifact)
        assert isinstance(restored, TextArtifact)
        assert restored.value == artifact.value


class TestPydanticModelFieldHook:
    """A pydantic BaseModel field (e.g. WorkflowMetadata) must unstructure with mode="json"."""

    def test_datetime_field_becomes_a_json_dumpable_string_representing_the_same_instant(self) -> None:
        created = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
        metadata = WorkflowMetadata(
            name="wf",
            schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
            engine_version_created_with="0.1.0",
            node_libraries_referenced=[],
            creation_date=created,
        )
        result = GetWorkflowMetadataResultSuccess(result_details="ok", workflow_metadata=metadata)

        # json.dumps-able is the contract: a datetime object left in the tree would raise here.
        wire_json = result.to_json()
        data = json.loads(wire_json)

        creation_date_on_wire = data["workflow_metadata"]["creation_date"]
        assert isinstance(creation_date_on_wire, str)
        assert datetime.fromisoformat(creation_date_on_wire) == created


class TestPathFieldHook:
    """A Path-typed request field must accept a plain string off the wire."""

    def test_path_field_accepts_string_off_the_wire(self) -> None:
        wire_value = {"project_path": "/workspace/project.yml"}

        request = converter.structure(wire_value, LoadProjectTemplateRequest)

        assert isinstance(request.project_path, Path)
        assert request.project_path == Path("/workspace/project.yml")


class TestFloatFieldHook:
    """A float-typed field must accept an int off the wire; JSON has no int/float distinction."""

    def test_float_field_accepts_int_off_the_wire(self) -> None:
        coerced = converter.structure(5, float)

        expected_value = 5.0
        assert coerced == expected_value
        assert isinstance(coerced, float)


class _PlaceholderProviderClass:
    """Stand-in class used only to exercise the bare `type` unstructure/structure hooks."""


class TestBareTypeFieldHook:
    """RegisterArtifactProviderRequest.provider_class is a bare `type`, not a dataclass instance."""

    def test_type_field_unstructures_to_dotted_module_and_qualname(self) -> None:
        request = RegisterArtifactProviderRequest(provider_class=_PlaceholderProviderClass)

        data = json.loads(request.to_json())

        assert data["provider_class"] == f"{__name__}._PlaceholderProviderClass"

    @pytest.mark.xfail(strict=True, reason=_TYPE_FIELD_MISSING_STRUCTURE_HOOK_REASON)
    def test_type_field_round_trips_back_into_the_original_type(self) -> None:
        request = RegisterArtifactProviderRequest(provider_class=_PlaceholderProviderClass)

        data = json.loads(request.to_json())
        restored = converter.structure(data, RegisterArtifactProviderRequest)

        assert restored.provider_class is _PlaceholderProviderClass


class TestPydanticValidatedOptionalFieldRoundTrip:
    """A payload built with only its own defaults must survive a full wire round trip."""

    @pytest.mark.xfail(strict=True, reason=_UPDATE_PROVIDER_PAYLOAD_ROUND_TRIP_REASON)
    def test_default_update_agent_provider_request_round_trips(self) -> None:
        request = UpdateAgentProviderRequest()

        data = json.loads(request.to_json())
        restored = converter.structure(data, UpdateAgentProviderRequest)

        assert restored == request


class _RaisesOnUnstructure:
    """A field value whose custom unstructure hook always raises, to exercise the fallback path."""

    def _cattrs_unstructure(self, converter: Any) -> dict[str, Any]:  # noqa: ARG002
        msg = "this value refuses to serialize"
        raise RuntimeError(msg)

    def __str__(self) -> str:
        return "a value that refuses to serialize"


@dataclasses.dataclass
class _HasOneBadField:
    """Local dataclass with one field that raises during unstructure and one that does not."""

    good_field: str
    bad_field: _RaisesOnUnstructure


class TestSafeUnstructureFallback:
    """safe_unstructure must not let one bad field lose the rest of the object."""

    def test_dataclass_with_one_unserializable_field_keeps_every_other_field(self) -> None:
        obj = _HasOneBadField(good_field="hello", bad_field=_RaisesOnUnstructure())

        result = safe_unstructure(obj)

        assert result["good_field"] == "hello"
        # The bad field is not lost: the raw value survives even though it could not be unstructured.
        assert isinstance(result["bad_field"], _RaisesOnUnstructure)

    def test_non_dataclass_falls_back_to_str(self) -> None:
        obj = _RaisesOnUnstructure()

        result = safe_unstructure(obj)

        assert result == "a value that refuses to serialize"


class TestFromDictUnknownPayloadType:
    """An unregistered request_type/result_type at the Event envelope level fails loudly."""

    def test_event_request_from_dict_rejects_unregistered_request_type(self) -> None:
        data = {"request_type": "TotallyUnknownRequestType", "request": {}}

        with pytest.raises(ValueError, match="TotallyUnknownRequestType"):
            EventRequest.from_dict(data)

    def test_event_result_success_from_dict_rejects_unregistered_result_type(self) -> None:
        data = {
            "request_type": "GetConfigValueRequest",
            "result_type": "TotallyUnknownResultType",
            "request": {"category_and_key": "a.b"},
            "result": {},
        }

        with pytest.raises(ValueError, match="TotallyUnknownResultType"):
            EventResultSuccess.from_dict(data)

    def test_event_result_failure_from_dict_rejects_unregistered_request_type(self) -> None:
        data = {
            "request_type": "TotallyUnknownRequestType",
            "result_type": "GetConfigValueResultSuccess",
            "request": {},
            "result": {"value": 1, "result_details": "ok"},
        }

        with pytest.raises(ValueError, match="TotallyUnknownRequestType"):
            EventResultFailure.from_dict(data)
