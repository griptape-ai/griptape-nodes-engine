- Updating a library no longer installs a version that needs a newer engine than the one running.
  Before, Library Management could still offer an update it had already found incompatible, and
  installing it left the library unable to load. Checking for updates on an engine too old for
  the newest version now reports the library as up to date instead of showing an error each time.
