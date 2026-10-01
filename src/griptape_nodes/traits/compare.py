from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from griptape_nodes.exe_types.core_types import Trait


@dataclass(eq=False)
class Compare(Trait):
    def converters_for_trait(self) -> list[Callable[[Any], Any]]:
        def convert(value: Any) -> Any:
            if isinstance(value, str):
                # How do i do a text diff here?
                pass

        return [convert]
