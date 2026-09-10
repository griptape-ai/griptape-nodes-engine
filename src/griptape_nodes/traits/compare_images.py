from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, Trait


@dataclass(eq=False)
class CompareImagesTrait(Trait):
    def __init__(self) -> None:
        super().__init__()

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["compare_images"]

    def validators_for_trait(self) -> list[Callable[[Parameter, Any], Any]]:
        def validate_image_comparison(parameter: Parameter, value: Any) -> Any:
            if not isinstance(value, dict):
                msg = f"Parameter {parameter.name} value must be a dictionary, got a {type(value).__name__} instead."
                raise TypeError(msg)

            expected_keys = {"input_image_1", "input_image_2"}
            actual_keys = set(value.keys())
            if actual_keys != expected_keys:
                missing = expected_keys - actual_keys
                extra = actual_keys - expected_keys
                details = []
                if missing:
                    details.append(f"missing keys: {sorted(missing)}")
                if extra:
                    details.append(f"unexpected keys: {sorted(extra)}")
                detail_msg = "; ".join(details)
                msg = f"Dictionary for Parameter '{parameter.name}' must contain exactly 'input_image_1' and 'input_image_2' keys; {detail_msg}"
                raise ValueError(msg)

            return value

        return [validate_image_comparison]
