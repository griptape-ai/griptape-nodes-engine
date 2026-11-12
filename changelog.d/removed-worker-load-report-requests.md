- `ReportLibraryLoadedRequest` and `ReportGeneralPurposeWorkerReadyRequest` are gone. A worker's
  load report now rides in its hello to the engine that started it, so no client sends either
  request and nothing replaces them on that side.
