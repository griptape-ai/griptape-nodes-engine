- **Breaking:** `StartFlowRequest` no longer accepts `wait_for_completion` or `completion_timeout_ms`.
  The request already answers once the run ends, so neither had any effect. Drop them from calls. To
  bound a run, wrap the call in `asyncio.wait_for` and send `CancelFlowRequest` on timeout.
