- Studios can run the engine inside an environment their own tools prepare, listing its libraries in
  the `GTN_LIBRARY_PATHS` environment variable. Setting `library.provisioned_by` to `environment`
  makes those the only libraries that load, with nothing downloaded and no virtual environments
  built. See [Running in a Managed Environment](https://docs.griptapenodes.com/en/stable/guides/managed_environment/).
