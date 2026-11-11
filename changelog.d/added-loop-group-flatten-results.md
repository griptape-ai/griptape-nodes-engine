- ForEach Group and ForLoop Group have "Flatten Results" and "Flatten Depth" settings in the group
  settings panel. With "Flatten Results" on, an iteration that adds a list puts its items into
  `results` instead of the list itself, so `results` comes out as one flat list. "Flatten Depth"
  sets how many levels to flatten (0 flattens every level). Strings and dicts stay whole.
  [#6315](https://github.com/griptape-ai/griptape-nodes-engine/issues/6315)
