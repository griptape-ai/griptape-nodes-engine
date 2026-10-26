- Saving a workflow right after updating one of its libraries now keeps parameter values whose
  type comes from that library, such as a node's mode dropdown. Before, the save dropped those
  values with a warning, and engines before 0.104.0 failed to save with "Missing stable
  namespace".
