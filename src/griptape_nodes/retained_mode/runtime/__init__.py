"""What one open workflow is: its runtime, the state it holds, and which runtime the current work acts on.

`WorkflowRuntime` holds one typed attribute per concern, each defined in a module of this package.
These are data and the operations on that data alone; managers hold the behavior that needs other
managers or events. `OpenWorkflowSet` holds the runtimes of the workflows the engine has open.
`binding` holds the runtime the current request and the tasks it spawns act on.
"""
