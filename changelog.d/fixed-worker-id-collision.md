- A GPU engine now refuses to start a worker for another engine under an ID already in use on it,
  whether that ID names a worker it already runs for a different engine, a remote start it is still
  waiting on, or its own engine ID.
