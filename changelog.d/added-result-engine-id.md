- Every request result now carries `engine_id` on its envelope, naming the engine that sent it, so
  a client that reaches an engine through a session topic knows which engine holds the session.
