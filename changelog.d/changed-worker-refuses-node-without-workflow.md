- A node whose library runs in a separate process now fails with "no workflow was open when it was
  sent" when its `ExecuteNodeRequest` names no open workflow (`orchestrator_workflow_id`), instead of
  running outside any workflow. Running a node from an open workflow always names it.
