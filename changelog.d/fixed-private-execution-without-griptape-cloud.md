- Node groups set to "Private Execution", and workflows published in a library's environment, now
  run without a Griptape Cloud connection. Their progress and results used to travel through
  Griptape Cloud, so these runs failed after a 10 second wait on licensed, offline, and air-gapped
  installs. They now stay on your machine in every mode. If such a run cannot connect back to the
  main engine, the error now says why, instead of reporting every cause as a timeout.
