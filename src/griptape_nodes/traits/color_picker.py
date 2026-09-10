from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from griptape_nodes.exe_types.core_types import Parameter, Trait


@dataclass(eq=False)
class ColorPicker(Trait):
    def __init__(self, format: Literal["hex", "hexa", "rgb", "rgba", "hsl", "hsla", "hsv", "hsva"] = "hex") -> None:  # noqa: A002
        super().__init__()
        self.format = format

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["color_picker"]

    def ui_options_for_trait(self) -> dict:
        return {"color_picker": {"format": self.format}}

    def _validate_hex_format(self, value: str) -> None:
        """Validate hex and hexa color formats."""
        if not value.startswith("#"):
            # Allow hex without # prefix
            if len(value) in [6, 8] and all(c in "0123456789ABCDEFabcdef" for c in value):
                return  # Valid hex without # prefix
            msg = f"Invalid {self.format} format: {value}. Expected format like #FF0000 or #FF000088"
            raise ValueError(msg)
        if self.format == "hex" and len(value) not in [4, 7]:  # #fff or #ffffff
            msg = f"Invalid hex format: {value}. Expected format like #FF0000 or #FFF"
            raise ValueError(msg)
        if self.format == "hexa" and len(value) not in [5, 9]:  # #ffff or #ffffffff
            msg = f"Invalid hexa format: {value}. Expected format like #FF000088 or #FFFF"
            raise ValueError(msg)

    def _validate_function_format(self, value: str, prefixes: tuple[str, ...], example: str) -> None:
        """Validate function-based color formats (rgb, hsl, hsv)."""
        if not value.startswith(prefixes):
            msg = f"Invalid {self.format} format: {value}. Expected format like {example}"
            raise ValueError(msg)

    def validators_for_trait(self) -> list[Callable[..., Any]]:
        def validate(param: Parameter, value: Any) -> None:  # noqa: ARG001
            if value is None:
                return

            if not isinstance(value, str):
                msg = f"Color value must be a string for format {self.format}"
                raise TypeError(msg)

            # Validate based on format
            if self.format in ["hex", "hexa"]:
                self._validate_hex_format(value)
            elif self.format in ["rgb", "rgba"]:
                self._validate_function_format(value, ("rgb(", "rgba("), "rgb(255, 255, 255)")
            elif self.format in ["hsl", "hsla"]:
                self._validate_function_format(value, ("hsl(", "hsla("), "hsl(0, 0%, 100%)")
            elif self.format in ["hsv", "hsva"]:
                self._validate_function_format(value, ("hsv(", "hsva("), "hsv(0, 0%, 100%)")

        return [validate]
