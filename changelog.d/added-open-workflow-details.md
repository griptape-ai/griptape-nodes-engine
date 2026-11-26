- Open workflows, as `ListOpenWorkflowsRequest`, the heartbeat, and the workflow events report them,
  now carry their `description`, thumbnail `image`, and `is_template` flag. A
  `WorkflowDetailsChangedEvent` tells every client when an open workflow's name, file, description,
  thumbnail, or template flag changes, whichever request changed it.
