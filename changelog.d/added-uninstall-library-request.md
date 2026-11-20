- `UninstallLibraryRequest` removes an installed node library: it unregisters the library,
  unloads it, and sends its folder to the recycle bin where the platform supports one.
  `PreviewUninstallLibraryRequest` reports what would be removed, and says when a library
  cannot be uninstalled, so the editor can disable the control before anything is clicked.
