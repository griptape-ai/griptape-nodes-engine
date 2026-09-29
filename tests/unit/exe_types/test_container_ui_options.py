from griptape_nodes.exe_types.core_types import ParameterList

GRID_COLUMNS = 3


def _grid_list() -> ParameterList:
    return ParameterList(
        name="items",
        tooltip="Items to render",
        input_types=["str"],
        collapsed=True,
        child_prefix="Item",
        grid=True,
        grid_columns=GRID_COLUMNS,
    )


class TestContainerLayoutIsAuthored:
    def test_the_save_view_carries_the_layout(self) -> None:
        parameter = _grid_list()

        assert parameter.authored_ui_options() == {
            "collapsed": True,
            "child_prefix": "Item",
            "display": "grid",
            "columns": GRID_COLUMNS,
        }

    def test_columns_are_omitted_without_grid_display(self) -> None:
        parameter = ParameterList(name="items", tooltip="t", input_types=["str"], grid_columns=GRID_COLUMNS)

        assert "display" not in parameter.authored_ui_options()
        assert "columns" not in parameter.authored_ui_options()

    def test_an_unrelated_write_keeps_the_layout(self) -> None:
        parameter = _grid_list()

        parameter.hide = True

        assert parameter.ui_options["display"] == "grid"
        assert parameter.ui_options["columns"] == GRID_COLUMNS
        assert parameter.ui_options["collapsed"] is True

    def test_an_unrelated_unset_keeps_the_layout(self) -> None:
        parameter = _grid_list()
        parameter.display_name = "Items"

        parameter.display_name = None

        assert parameter.ui_options["display"] == "grid"
        assert parameter.ui_options["child_prefix"] == "Item"

    def test_a_collapse_is_reported_as_a_difference(self) -> None:
        declared = ParameterList(name="items", tooltip="t", input_types=["str"], collapsed=False)
        live = ParameterList(name="items", tooltip="t", input_types=["str"], collapsed=False)

        live.collapsed = True

        assert declared.equals(live) == {"ui_options": {"collapsed": True}}
