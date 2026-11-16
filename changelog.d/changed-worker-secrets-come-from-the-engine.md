- **Breaking:** In a library's worker process, `GRIPTAPE_NODES_LICENSE` and `GT_CLOUD_API_KEY` are
  empty in `os.environ`, and setting a secret fails. Read secrets with
  `GriptapeNodes.SecretsManager().get_secret(...)`, and pass a Griptape Cloud driver its key from
  `resolve_cloud_credential`. See
  [Node isolation with workers](docs/development/custom_nodes/node_isolation_with_workers.md#secrets-and-griptape-cloud-credentials).
