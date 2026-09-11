"""What a trait's saved state may hold, and the one declaration of a saved trait's shape."""

from __future__ import annotations

from pathlib import Path

from griptape_nodes.exe_types.trait_state import TraitStateEntry, as_saved_state_value


class TestValuesASavedArtifactCanHold:
    def test_a_scalar_is_kept_as_it_is(self) -> None:
        for value in (None, True, 3, 2.5, "text"):
            saved = as_saved_state_value(value)

            assert saved.unsupported_type is None
            assert saved.value == value

    def test_a_nested_list_and_dict_are_kept(self) -> None:
        value = {"choices": ["a", "b"], "layout": {"columns": 2, "grid": True}}

        saved = as_saved_state_value(value)

        assert saved.unsupported_type is None
        assert saved.value == value

    def test_a_tuple_becomes_a_list(self) -> None:
        saved = as_saved_state_value((1, 2))

        assert saved.unsupported_type is None
        assert saved.value == [1, 2]

    def test_a_set_becomes_a_sorted_list_so_repeated_saves_match(self) -> None:
        """String hashing differs run to run, so an unsorted set would shuffle the file."""
        saved = as_saved_state_value({".mp4", ".avi", ".mov"})

        assert saved.unsupported_type is None
        assert saved.value == [".avi", ".mov", ".mp4"]


class TestValuesItCannot:
    def test_an_arbitrary_object_is_reported_by_type(self) -> None:
        saved = as_saved_state_value(Path("/tmp/somewhere"))  # noqa: S108

        assert saved.unsupported_type == "PosixPath"

    def test_an_unsupported_value_inside_a_container_is_reported(self) -> None:
        saved = as_saved_state_value({"paths": [Path("/tmp/somewhere")]})  # noqa: S108

        assert saved.unsupported_type == "PosixPath"

    def test_a_dictionary_keyed_by_anything_but_text_is_reported(self) -> None:
        """A saved mapping has text keys, so an int-keyed dict would come back changed."""
        saved = as_saved_state_value({1: "one"})

        assert saved.unsupported_type == "dictionary keyed by int"


class TestTheSavedShape:
    def test_a_full_entry_round_trips(self) -> None:
        entry = TraitStateEntry(
            trait_name="Button",
            trait_module="griptape_nodes.traits.button",
            trait_state={"label": "Refresh"},
            trait_callbacks={"on_click": "refresh"},
        )

        assert TraitStateEntry.from_dict(entry.to_dict()) == entry

    def test_the_callbacks_key_is_absent_when_there_are_none(self) -> None:
        entry = TraitStateEntry(trait_name="Options", trait_module="griptape_nodes.traits.options")

        assert entry.to_dict() == {
            "trait_name": "Options",
            "trait_module": "griptape_nodes.traits.options",
            "trait_state": {},
        }

    def test_an_entry_naming_no_trait_reads_as_nothing(self) -> None:
        assert TraitStateEntry.from_dict({"trait_state": {"label": "Refresh"}}) is None

    def test_a_missing_module_reads_as_absent_rather_than_guessed(self) -> None:
        entry = TraitStateEntry.from_dict({"trait_name": "Options"})

        assert entry is not None
        assert entry.trait_module is None

    def test_a_missing_state_reads_as_empty(self) -> None:
        """An entry that says nothing about state asks for whatever the node's code builds."""
        entry = TraitStateEntry.from_dict({"trait_name": "Options", "trait_module": "m"})

        assert entry is not None
        assert entry.trait_state == {}
        assert entry.trait_callbacks == {}
