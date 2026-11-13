- `UninstallLibraryRequest` and `PreviewUninstallLibraryRequest` remove a node library from the
  engine: its `libraries_to_register`/`libraries_to_download` entry, its registry load, and its
  install folder, sent to the recycle bin where the platform supports one. A library the engine
  cannot fully remove is refused rather than partly uninstalled, and the preview reports that
  before anything is clicked: one whose entry lives in a project, workspace, or environment
  config the engine does not write, and one whose registration it cannot find at all. Where a
  `libraries_to_register` entry names a folder the engine scans, only the library's own folder
  is removed and the entry stays.
