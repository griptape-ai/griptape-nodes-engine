- `UninstallLibraryRequest` and `PreviewUninstallLibraryRequest` remove a node library from the
  engine: its `libraries_to_register`/`libraries_to_download` entry, its registry load, and its
  install folder, sent to the recycle bin where the platform supports one. Where a
  `libraries_to_register` entry names a folder the engine scans, only the library's own folder is
  removed and the entry stays. The preview reports what the uninstall would delete, and why it is
  refused or what would still register the library afterwards.
