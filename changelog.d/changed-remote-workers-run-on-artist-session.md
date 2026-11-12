- A library's nodes sent to another computer with `distributed_execution.library_engines` now run
  on your workstation's license session instead of that computer's license: your license decides
  what they may do, and their Griptape Cloud calls authenticate as your session. A workstation with
  a Griptape Cloud API key and no license still uses the other computer's `GT_CLOUD_API_KEY` for
  those calls. Two computers can now each run a library for the other.
