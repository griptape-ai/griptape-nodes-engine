- A library's worker process no longer stops while it starts because the engine it works for is
  busy for more than 30 seconds, which logged "This engine's role failed fatally" and left the
  library's nodes unable to run. A starting worker now waits for as long as that engine is running.
