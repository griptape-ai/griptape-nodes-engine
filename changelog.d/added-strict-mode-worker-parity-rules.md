- Three strict-mode rules flag node code that behaves differently in a separate process than
  in-process: `graph-access-while-loading` (asking about connections or other nodes while the node
  loads), `state-read-bypassing-requests` (reading a manager instead of sending a request), and
  `state-outside-parameters` (keeping state set by `after_value_set` or a connection hook on the
  node instead of in a parameter). Each warns where the node runs in-process and fails the node's run
  in a separate process.
  [#5955](https://github.com/griptape-ai/griptape-nodes-engine/issues/5955)
