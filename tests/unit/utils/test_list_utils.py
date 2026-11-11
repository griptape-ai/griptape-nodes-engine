from griptape_nodes.utils.list_utils import flatten_list


class TestFlattenList:
    def test_depth_zero_flattens_every_level(self) -> None:
        assert flatten_list([1, [2, [3, [4]]]], 0) == [1, 2, 3, 4]

    def test_depth_one_flattens_one_level(self) -> None:
        assert flatten_list([1, [2, [3]]], 1) == [1, 2, [3]]

    def test_default_depth_flattens_every_level(self) -> None:
        assert flatten_list([[1], [[2]]]) == [1, 2]

    def test_strings_and_dicts_stay_whole(self) -> None:
        assert flatten_list(["ab", {"a": 1}, ["cd"]], 0) == ["ab", {"a": 1}, "cd"]
