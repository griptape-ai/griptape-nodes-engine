- The editor no longer sometimes shows a workflow's state from partway through an operation, such as
  its unsaved-changes marker, when part of that operation runs on another thread. The engine now
  sends events in the order they happen.
