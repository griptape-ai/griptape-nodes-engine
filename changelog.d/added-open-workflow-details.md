- Open workflows, as `ListOpenWorkflowsRequest`, the heartbeat, and the workflow events report them,
  now carry their `description`, thumbnail `image`, `is_template`, and `is_griptape_provided`. A
  `WorkflowDetailsChangedEvent` tells every client when an open workflow's name, file, description,
  thumbnail, or template flag changes, whichever request changed it.
