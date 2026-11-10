- `UninstallLibraryRequest` and `PreviewUninstallLibraryRequest` remove a node library from the
  engine: its `libraries_to_register`/`libraries_to_download` entry, its registry load, and its
  install folder, sent to the recycle bin where the platform supports one. The preview reports
  what the uninstall would delete, including other libraries in the same folder, and why it is
  refused or what would still register the library afterwards.
