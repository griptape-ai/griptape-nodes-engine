- `RunArbitraryPythonStringRequest` failures now start with the exception type, such as
  `ZeroDivisionError: division by zero`, instead of `ERROR: division by zero`. This is the text a
  node that runs Python code shows when the code fails.
  [#5733](https://github.com/griptape-ai/griptape-nodes-engine/issues/5733)
