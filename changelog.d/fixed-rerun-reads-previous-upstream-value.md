- Running a flow a second time now makes a node wait for an upstream node that the run is about
  to execute again, instead of starting beside it. Before, a node fed by a node in another control
  chain could run on the first run's value, or fail with an empty input such as "Connect an image
  to 'Input Image'", on every run after the first.
