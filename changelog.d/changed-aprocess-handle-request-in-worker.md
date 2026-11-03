- **Breaking:** In a worker, `handle_request` called from a node's `aprocess()` now fails instead of
  blocking the engine until the orchestrator replies. Send requests from `aprocess()` with
  `await GriptapeNodes.ahandle_request(...)`.
