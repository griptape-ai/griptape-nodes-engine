- Scripts that enter a workflow by a name they never registered, such as
  `push_workflow(workflow_name="my_flow")`, no longer log an "Optional builtin 'workflow_dir'
  could not be resolved" warning for every project directory. `workflow_dir` now answers with the
  folder the workflow's first save would land in, as it does for any unsaved workflow.
