- **Breaking:** Parameter values the engine sends to the editor and to request API clients name
  their type when JSON has no such type, so a tuple arrives as
  `{"$type": "builtins:tuple", "$value": [1, 2]}` instead of a list. A value sent back in the same
  form is set with that exact type.
  [Parameter values](docs/guides/mcp/external_clients.md#parameter-values) shows the form values
  take and which fields carry them. See
  [MIGRATION.md](MIGRATION.md#parameter-values-carry-their-type).
