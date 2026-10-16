- An "Isolated Execution in a worker" beta feature runs the nodes of groups and loops set to
  "Isolated Execution" in one background process that stays open for the session, instead of
  starting a new process each time the group or loop runs. The group's results no longer travel
  through Griptape Cloud, so Isolated Execution works on installs that can't reach it. Turn it on
  from the editor's Beta settings page.
