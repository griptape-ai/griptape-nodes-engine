- A request a node library makes with `GriptapeNodes.handle_request` from a thread, such as a step
  a node's `process()` yields or a thread the library starts, is now handled on the engine's event
  loop while the thread waits. Handlers that start background work no longer lose it when the
  request finishes.
