# Budgets for Direct Provider Calls

Griptape Cloud enforces spending budgets on every call that goes through the Griptape proxy. A node that calls a model provider directly, for example with the user's own OpenAI or Anthropic key, bypasses the proxy, so Cloud never sees the call. Two requests let such a node take part in budgets anyway:

- `BudgetAccessRequest`, sent **before** the call, asks Cloud whether the call fits the budgets of the open project.
- `ReportUsageRequest`, sent **after** the call, tells Cloud what the call cost, so it counts against those budgets and shows in usage auditing.

The engine adds the project, workflow, engine and session to both requests for you. Send the provider's own name for the model, such as `gpt-5-nano`, in both requests, so a refusal and the spend it would have been are grouped under the same model in usage audits. Pass the node's class name as `node_type`. `node_id` is for an opaque id; never pass the node's name, which the user types and may not want shared.

## Before the call: `BudgetAccessRequest`

```python
from griptape_nodes.retained_mode.events.budget_events import (
    BudgetAccessRequest,
    BudgetAccessResultFailure,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

check = await GriptapeNodes.ahandle_request(
    BudgetAccessRequest(
        model="gpt-5-nano",
        estimated_cost_micro_usd=40_000,
        node_type=type(self).__name__,
    )
)
if isinstance(check, BudgetAccessResultFailure):
    raise check.exception
```

- In `aprocess()`, `await GriptapeNodes.ahandle_request(...)` as shown, so the engine keeps running while the check waits on Griptape Cloud. In a synchronous `process()`, call `GriptapeNodes.handle_request(...)` instead.
- A **failure** means a budget refused the call. Do not make it. Raise `check.exception`: the run stops with a message naming the node and every budget that refused, and the editor shows its "Run blocked" bar.
- A **success** means go ahead. `checked` is `False` when Cloud could not be reached or no Griptape Cloud credential is set; the call is still cleared. A network problem must never stop work.
- `estimated_cost_micro_usd` is optional. Without it, only a budget with no headroom left refuses the call.

## After the call: `ReportUsageRequest`

Report the token counts the provider returned. Griptape Cloud prices them, so your node never needs a price table.

```python
from griptape_nodes.retained_mode.events.budget_events import ReportUsageRequest

await GriptapeNodes.ahandle_request(
    ReportUsageRequest(
        input_tokens=1_000,
        output_tokens=500,
        cache_read_tokens=900,
        provider="openai",
        model="gpt-5-nano",
        activity_type="chat_completion",
        node_type=type(self).__name__,
        correlation_id=check.correlation_id,
    )
)
```

- `input_tokens` is the **total** prompt token count, cached tokens included. `cache_read_tokens` is the cached share of it, and `cache_write_tokens` the tokens written to the cache. Leave either out when the provider doesn't report it. Providers report the total differently:

    | Provider           | `input_tokens`                                                             | `cache_read_tokens`                   | `cache_write_tokens`                       |
    | ------------------ | -------------------------------------------------------------------------- | ------------------------------------- | ------------------------------------------ |
    | OpenAI             | `prompt_tokens`                                                            | `prompt_tokens_details.cached_tokens` | `prompt_tokens_details.cache_write_tokens` |
    | Anthropic          | `input_tokens` + `cache_read_input_tokens` + `cache_creation_input_tokens` | `cache_read_input_tokens`             | `cache_creation_input_tokens`              |
    | Bedrock (Converse) | `inputTokens` + `cacheReadInputTokens` + `cacheWriteInputTokens`           | `cacheReadInputTokens`                | `cacheWriteInputTokens`                    |

    Anthropic and Bedrock leave the cached tokens out of their input count, so passing their input count alone reports too little, and Griptape Cloud rejects a report whose cache counts exceed it.

- When the call took audio in, also send `input_audio_tokens`, the audio share of `input_tokens` with cached audio included, and `cache_audio_read_tokens`, the cached share of that audio. The cached audio is also counted in `cache_read_tokens`. OpenAI reports input audio as `prompt_tokens_details.audio_tokens`. Leave both out for a call without audio.

- If your node runs its model through pydantic-ai, pass its `RunUsage` counts straight across, audio included: `RunUsage.input_tokens` is already the total for every provider, and `input_audio_tokens` and `cache_audio_read_tokens` follow the same rules.

- `model` is required with token counts, because Griptape Cloud prices by model. Send the same provider name for the model as in the check.

- Token counts are priced as chat completions. For a chat call, leave `activity_type` out or set it to `chat_completion`. For any other kind of call, send `declared_cost_micro_usd` instead.

- If your node knows its cost but has no token counts, send `declared_cost_micro_usd` (in millionths of a US dollar) instead. Send one or the other, never both.

- The report is sent in the background and returns at once. It is retried briefly, then dropped with a line in the engine log.

- **Never fail a node over its report.** Ignore the result. A report with neither token counts nor a declared cost, or with both, comes back as a failure with the reason. Griptape Cloud checks everything else, such as the model or the cache counts, and a report it rejects is logged and dropped. Either way the node carries on.

- Pass the `correlation_id` from the check's success so Cloud can pair the check with the report.
