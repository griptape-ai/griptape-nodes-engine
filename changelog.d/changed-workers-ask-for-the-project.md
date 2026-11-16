- A library's worker now asks the engine it works for which project it is on, when it starts and
  whenever that engine switches projects, instead of being sent the project. Several switches in
  quick succession cost the worker at most two project changes, and it lands on the newest.
  `ActivateProjectRequest` no longer has `project_id` or `generation`.
