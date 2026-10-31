- Node libraries can declare limits on a parameter's value with `pre_run_validators=`, from
    `griptape_nodes.exe_types.validators`: `MaxCount` and `IfParameter` for any value, and limits on
    image, video and audio files such as `AllowedFormats`, `MaxFileSize`, `SideRange` or
    `MaxDuration`. The engine checks them at the node's turn and fails the node before it runs, with
    an error that names the parameter, the value and the limit, and links the page from the node's
    new `input_limits_url()`. A library can write its own: subclass `PreRunValidator` for a limit on
    any value, or `MediaValidator` for one judged on the file. `limit_error` builds the same error for
    a rule checked in `validate_before_node_run`, and `validate_node` runs a node's checks as the
    engine does, for tests. A media input's format, codec, size, pixel size, length, frame rate and
    frame count are read locally, once; anything that can't be read locally, such as a remote URL,
    is left for the provider, and a video or audio input that takes more than 3 seconds to read is
    skipped and logged as a warning. See
    [Checking inputs before a run](https://docs.griptapenodes.com/en/stable/development/custom_nodes/error_handling/#checking-inputs-before-a-run).
    [#5960](https://github.com/griptape-ai/griptape-nodes-engine/issues/5960)
