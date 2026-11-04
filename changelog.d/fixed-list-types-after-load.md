- Saving or copying a node keeps the item types of its lists. A list whose type a node set while
  running, such as Display List's, loads as `list[Thing]` instead of `list[list[Thing]]`, and a row
  the node typed on its own, such as an `int` row beside `str` rows, keeps its value instead of
  failing to load. Workflows saved before this fix still load their list types as saved.
