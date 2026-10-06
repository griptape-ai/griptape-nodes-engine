- The sidebar agent no longer fails with "Request timed out" behind proxies that hold the TLS
  handshake for longer than 5 seconds. Model requests now allow 60 seconds to connect.
  [#5768](https://github.com/griptape-ai/griptape-nodes-engine/issues/5768)
