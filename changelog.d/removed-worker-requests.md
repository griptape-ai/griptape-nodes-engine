- The requests a library's worker used to send its engine are gone: `RegisterWorkerRequest`,
  `UnregisterWorkerRequest`, `WorkerHeartbeatRequest`, `ReportLibraryLoadedRequest` and
  `ReportGeneralPurposeWorkerReadyRequest`. A worker is now its engine's own child process: it
  reports what it loaded in its hello, and the engine knows it stopped the moment its process does.
  No client sends any of these, and nothing replaces them.
