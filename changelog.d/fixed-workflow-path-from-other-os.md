- `ImportWorkflowRequest` and `LoadWorkflowMetadata` now fail with an error saying the path comes
  from a different operating system when given an absolute path from another OS, such as
  `/Volumes/...` sent to a Windows engine or `\\host\...` sent to a macOS or Linux engine. They
  previously joined it onto the workspace and reported that no file existed at the combined path.
  [#5712](https://github.com/griptape-ai/griptape-nodes-engine/issues/5712)
