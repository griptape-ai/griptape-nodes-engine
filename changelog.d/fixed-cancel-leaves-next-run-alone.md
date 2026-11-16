- Cancelling a run that ends on its own while the cancel is still stopping its nodes no longer
  stops the next waiting run: that run's nodes now finish, and the editor is no longer told the
  flow was cancelled.
