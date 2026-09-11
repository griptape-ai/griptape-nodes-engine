from collections.abc import Callable
from typing import Any

from griptape_nodes.exe_types.core_types import Trait


class Compare(Trait):
    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["compare"]

    def converters_for_trait(self) -> list[Callable[[Any], Any]]:
        def convert(value: Any) -> Any:
            if isinstance(value, str):
                # How do i do a text diff here?
                pass

        return [convert]
