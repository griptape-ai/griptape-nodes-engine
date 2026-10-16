- A node whose library runs in a separate worker process no longer waits up to 10 minutes when
  that process stops while starting up, for example because no license session was available
  for it. The run now fails right away and says that the process stopped and where to look.
