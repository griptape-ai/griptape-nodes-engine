- Node libraries can open a file in the viewer application a user picked in the library's settings
  by sending `LaunchExternalViewerRequest`. The engine starts `<category>.viewer_executable` with
  the arguments in `<category>.viewer_args` followed by the file, and does not wait for it. When
  `fallback_to_os_default` is set and no viewer is chosen, it opens the file with the system's
  default application instead. Requests sent from a worker open the viewer on the user's machine.
  [#4942](https://github.com/griptape-ai/griptape-nodes-engine/issues/4942)
