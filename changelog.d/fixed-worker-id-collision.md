- A GPU engine now refuses to start a worker for another engine under an ID already in use on it,
  whether that ID names a worker it already hosts, a worker registered directly with it, a remote
  start it is already waiting on, or its own engine ID.
