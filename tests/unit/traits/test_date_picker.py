from typing import Any

import pytest

from griptape_nodes.exe_types.core_types import Parameter
from griptape_nodes.traits.date_picker import DatePicker


def _parameter(trait: DatePicker) -> Parameter:
    return Parameter(name="when", type="str", tooltip="t", traits={trait})


def _validate(trait: DatePicker, value: Any) -> None:
    parameter = _parameter(trait)
    for validator in trait.validators_for_trait():
        validator(parameter, value)


class TestUiOptions:
    def test_date_only_omits_unset_bounds(self) -> None:
        assert DatePicker().ui_options_for_trait() == {"date_picker": {"include_time": False}}

    def test_bounds_are_rendered_when_set(self) -> None:
        trait = DatePicker(include_time=True, min="2026-01-01T00:00", max="2026-12-31T23:59")

        assert trait.ui_options_for_trait() == {
            "date_picker": {"include_time": True, "min": "2026-01-01T00:00", "max": "2026-12-31T23:59"}
        }

    def test_an_editor_write_is_adopted(self) -> None:
        trait = DatePicker()
        parameter = _parameter(trait)

        parameter.ui_options = {"date_picker": {"include_time": True, "max": "2026-12-31T23:59"}}

        assert (trait.include_time, trait.max) == (True, "2026-12-31T23:59")

    def test_an_editor_write_turning_time_off_with_a_date_time_bound_is_refused(self) -> None:
        trait = DatePicker(include_time=True, max="2026-12-31T23:59")
        parameter = _parameter(trait)

        parameter.ui_options = {"date_picker": {"include_time": False}}

        assert (trait.include_time, trait.max) == (True, "2026-12-31T23:59")
        _validate(trait, "2026-10-14T09:30")

    def test_an_editor_write_with_a_malformed_bound_is_refused(self) -> None:
        trait = DatePicker(min="2026-01-01")
        parameter = _parameter(trait)

        parameter.ui_options = {"date_picker": {"include_time": False, "min": "01/01/2026"}}

        assert trait.min == "2026-01-01"
        _validate(trait, "2026-10-14")

    def test_apply_state_rejects_a_bound_that_does_not_parse(self) -> None:
        trait = DatePicker(include_time=True, min="2026-01-01T00:00")

        with pytest.raises(ValueError, match="min '2026-01-01T00:00'"):
            trait.apply_state({"include_time": False})

        assert (trait.include_time, trait.min) == (True, "2026-01-01T00:00")

    @pytest.mark.parametrize("state", [{"min": 20260101}, {"include_time": "false"}])
    def test_apply_state_rejects_values_of_the_wrong_type(self, state: dict[str, Any]) -> None:
        trait = DatePicker(min="2026-01-01")

        with pytest.raises(TypeError):
            trait.apply_state(state)

        assert (trait.include_time, trait.min) == (False, "2026-01-01")

    def test_state_round_trips_through_the_constructor(self) -> None:
        trait = DatePicker(include_time=True, min="2026-01-01T00:00")

        rebuilt = DatePicker.from_state(trait.to_state())

        assert rebuilt.to_state() == trait.to_state()


class TestValidation:
    @pytest.mark.parametrize("value", [None, "", "2026-10-14", "2026-02-28"])
    def test_date_only_accepts(self, value: Any) -> None:
        _validate(DatePicker(), value)

    @pytest.mark.parametrize("value", ["2026-10-14T09:30", "2026-1-5", "14/10/2026", "2026-02-30", "20261014"])
    def test_date_only_rejects_other_shapes(self, value: str) -> None:
        with pytest.raises(ValueError, match="not a date in the form 2026-10-14"):
            _validate(DatePicker(), value)

    def test_with_time_accepts_minutes(self) -> None:
        _validate(DatePicker(include_time=True), "2026-10-14T09:30")

    @pytest.mark.parametrize("value", ["2026-10-14", "2026-10-14T09:30:00", "2026-10-14T09:30+02:00"])
    def test_with_time_rejects_other_shapes(self, value: str) -> None:
        with pytest.raises(ValueError, match="2026-10-14T09:30"):
            _validate(DatePicker(include_time=True), value)

    def test_a_non_string_is_rejected(self) -> None:
        with pytest.raises(TypeError, match="must be text"):
            _validate(DatePicker(), 20261014)

    def test_bounds_are_inclusive(self) -> None:
        trait = DatePicker(min="2026-01-01", max="2026-12-31")

        _validate(trait, "2026-01-01")
        _validate(trait, "2026-12-31")

    def test_a_value_before_min_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="earlier than 2026-01-01"):
            _validate(DatePicker(min="2026-01-01"), "2025-12-31")

    def test_a_value_after_max_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="later than 2026-12-31"):
            _validate(DatePicker(max="2026-12-31"), "2027-01-01")

    def test_a_malformed_bound_is_reported(self) -> None:
        with pytest.raises(ValueError, match="max '31/12/2026'"):
            _validate(DatePicker(max="31/12/2026"), "2026-10-14")

    def test_a_date_only_bound_covers_its_whole_day_with_time(self) -> None:
        trait = DatePicker(include_time=True, min="2026-01-01", max="2026-12-31")

        _validate(trait, "2026-01-01T00:00")
        _validate(trait, "2026-12-31T23:59")
        with pytest.raises(ValueError, match="later than 2026-12-31"):
            _validate(trait, "2027-01-01T00:00")
