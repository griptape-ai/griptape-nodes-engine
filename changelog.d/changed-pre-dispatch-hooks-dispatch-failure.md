- **Breaking:** A pre-dispatch hook registered with `EventManager.add_pre_dispatch_hook` must refuse a
  request with a `DispatchFailure` subclass, not any result. The failures the engine returns for any
  request (`GenericResultFailure`, `WorkflowNotOpenResultFailure`, `WorkflowNotNamedResultFailure`)
  are now `DispatchFailure`s, so `try_request` can return them alongside the request's own results.
