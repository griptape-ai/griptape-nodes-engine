- A workflow run from the command line, or a node group run in its own process, now waits up to 30
  seconds before exiting for its direct provider calls' usage reports to reach Griptape Cloud.
  Before, a call made just before the run ended could go unreported, so its spend never counted
  against budgets.
  [#5958](https://github.com/griptape-ai/griptape-nodes-engine/issues/5958)
