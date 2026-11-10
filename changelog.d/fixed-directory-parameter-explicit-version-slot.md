- A directory parameter set to an absolute, `~`, or `file://` folder containing `{###}`, such as
  `/renders/shot_v{###}`, now creates the next free version (`shot_v001`, `shot_v002`, ...)
  instead of failing with "missing required variables: _index". A folder without `{###}` is still
  used exactly as given.
  [griptape-nodes-library-standard#945](https://github.com/griptape-ai/griptape-nodes-library-standard/issues/945)
