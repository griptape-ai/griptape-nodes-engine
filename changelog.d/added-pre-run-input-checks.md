- Node libraries can declare the limits a provider puts on an image, video or audio input with
    `pre_run_validators=` on the parameter, such as `AllowedFormats`, `MaxFileSize`, `SideRange` or
    `Duration` from `griptape_nodes.exe_types.validators`. The engine checks them at the node's
    turn and fails the node before it runs, with an error that names the parameter, the value
    and the limit, and links the page from the node's new `input_limits_url()`. It reads each input's
    format, codec, size, pixel size, length, frame rate and frame count locally, once; anything it
    can't read locally, such as a remote URL, is left for the provider; a video or audio input that
    takes more than 3 seconds to read is skipped and logged as a warning. See
    [Checking inputs before a run](https://docs.griptapenodes.com/en/stable/development/custom_nodes/error_handling/#checking-inputs-before-a-run).
    [#5960](https://github.com/griptape-ai/griptape-nodes-engine/issues/5960)
