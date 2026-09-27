from griptape_nodes.exe_types.core_types import Parameter
from griptape_nodes.exe_types.param_types.parameter_number import ParameterNumber
from griptape_nodes.traits.minmax import MinMax


class TestRenderedOptions:
    def test_renders_nothing(self) -> None:
        assert MinMax(min_val=0, max_val=10).ui_options_for_trait() == {}

    def test_range_validation_does_not_make_a_parameter_multiline(self) -> None:
        param = ParameterNumber(
            name="width",
            type="int",
            output_type="int",
            min_val=0,
            max_val=10,
            validate_min_max=True,
            tooltip="test",
        )

        assert any(isinstance(trait, MinMax) for trait in param.find_elements_by_type(MinMax))
        assert "multiline" not in param.ui_options

    def test_attaching_the_trait_leaves_an_authored_multiline_alone(self) -> None:
        param = Parameter(
            name="notes",
            input_types=["str"],
            type="str",
            output_type="str",
            tooltip="test",
            ui_options={"multiline": True},
        )

        param.add_trait(MinMax(min_val=0, max_val=10))

        assert param.ui_options["multiline"] is True
