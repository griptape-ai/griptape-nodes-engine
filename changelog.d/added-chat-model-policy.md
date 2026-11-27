- Admin model policies now apply to the chat sidebar agent, not just node model dropdowns: the
  same `OfferModel` / `InvokeModel` license rules gate chat and image models across all four
  providers, including Ollama, LM Studio, and custom endpoints. A license on a closed
  (deny-by-default) policy loses any chat model its policies don't permit.
  [#6446](https://github.com/griptape-ai/griptape-nodes-engine/issues/6446)
