- Scanning workflows (project switch, engine start, registry refresh) now sends at most ten
  `EngineInitializationProgress` events a second instead of two per workflow. Failures and the
  final event are still sent, so clients that counted every per-workflow event receive fewer.
