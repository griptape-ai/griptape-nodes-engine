- **Breaking:** `ControlFlowResolvedEvent` names the type of each value in `parameter_output_values`
  when JSON has no such type, so a tuple arrives as `{"$type": "builtins:tuple", "$value": [1, 2]}`
  instead of a list. A value with no plain-data form arrives as `null` instead of as its text, and
  the event no longer has `unique_parameter_uuid_to_values`.
