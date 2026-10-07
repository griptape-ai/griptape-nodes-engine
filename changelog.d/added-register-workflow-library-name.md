- `RegisterWorkflowRequest` takes a `library_name`, which ties the entry to that library: it goes
  away when the library unloads, and survives a workspace rescan. Leave it unset for workflows the
  user creates.
