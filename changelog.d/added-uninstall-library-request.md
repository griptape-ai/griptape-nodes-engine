- `UninstallLibraryRequest` and `PreviewUninstallLibraryRequest` remove a node library from the
  engine: its `libraries_to_register`/`libraries_to_download` config entry, its registry load, and
  its install folder (sent to the recycle bin where supported). The preview resolves the exact
  same target the real uninstall deletes and returns a `resolution_fingerprint` the uninstall
  requires, so a library installed, moved, or re-registered while the confirmation is open cannot
  be acted on against what was shown. Both report structured verdicts rather than prose:
  `blocked_by` says why an uninstall is refused (environment-managed, the Sandbox Library, or an
  unsafe path such as a filesystem root, a drive root, a UNC share, or a `.`/`..` segment), and
  `remaining_registrations` says what would still register the library afterwards (a shared
  directory entry and the libraries it also registers, a `libraries_to_download` git URL, or a
  project/workspace/environment config layer the engine cannot write to). Both also list any
  other library inside the install folder, including ones whose manifest fails to parse.
