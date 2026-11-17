- A workflow run from the command line, or a group or loop set to "Isolated Execution", now waits
  before exiting for its direct provider calls' usage reports to reach Griptape Cloud: up to 30
  seconds for a command-line run, up to 10 for an "Isolated Execution" group. Before, a call made
  just before the run ended could go unreported, so its spend never counted against budgets.
  [#5958](https://github.com/griptape-ai/griptape-nodes-engine/issues/5958)
