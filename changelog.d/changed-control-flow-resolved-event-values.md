- **Breaking:** `ControlFlowResolvedEvent` and `SerializeFlowToCommandsResultSuccess` name the type
  of each parameter value they carry when JSON has no such type, so a tuple arrives as
  `{"$type": "builtins:tuple", "$value": [1, 2]}` instead of a list. In `ControlFlowResolvedEvent`, a
  value with no plain-data form arrives as `null` instead of as its text, and the event no longer
  has `unique_parameter_uuid_to_values`.
