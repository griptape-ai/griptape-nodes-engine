- Parameter values now accept function calls inside `{}` tokens, such as
  `{path_join(outputs, "renders", name)}`, `{with_ext(name, "jpg")}`, `{now("%Y%m%d")}`, and
  `{random(1, 10)}`. A fixed set of path, date, random, text, and number functions is available.
  In `path_join`, `..` cannot climb out of the first path, and `temp_dir` and `temp_file` always
  point inside the workspace. A call with the wrong arguments fails the node with a message naming
  the function, and text naming a function that does not exist, such as `{foo()}`, is left as
  written. `ListVariablesResultSuccess` and `ListSubstitutablesResultSuccess` gain a `functions`
  list with each function's name, signature, and description.
