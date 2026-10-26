- Node libraries can now give parameters an `adaptive_type_group`. All parameters on a node in the
  same group take the type of the first connection made to any of them, reject connections that
  can't carry that type, and go back to `any` when their last connection is removed. See
  [Type-Adaptive Parameters](docs/development/custom_nodes/parameters.md#type-adaptive-parameters).
