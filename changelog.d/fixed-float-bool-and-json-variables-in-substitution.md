- Float, bool, and JSON (dict) variables now substitute into `{VAR}` tokens and appear in the
  variable picker. Before, a `{VAR}` token for one of these stayed in the text as typed. A float
  renders as `1.5`, a bool as `true` or `false`, and a dict as compact JSON.
  [#5839](https://github.com/griptape-ai/griptape-nodes-engine/issues/5839)
