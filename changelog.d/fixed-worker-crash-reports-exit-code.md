- A node whose library worker crashes, for example because it ran out of memory, now fails as soon as
  the worker exits, with the exit code and its likely cause. It no longer waits for the worker to miss
  its heartbeats and then reports that the worker "stopped responding and was shut down".
