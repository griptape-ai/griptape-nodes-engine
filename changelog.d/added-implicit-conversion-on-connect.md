- Node libraries can now set `implicit_conversion=True` on an input so it also accepts connections
  it can convert: `int` and `float` to each other, numbers and `bool` to `str`, `dict` and `list` to
  and from JSON, `str` to numbers, and outputs typed `any` (converted by the value that arrives). The value is converted as it arrives, a value that can't be
  converted stops the run with an error naming where it came from, and the connection's new
  `conversion` field lets the editor draw it differently. Inputs without the setting are unchanged.
  See [Implicit Conversion](docs/development/custom_nodes/parameters.md#implicit-conversion).
