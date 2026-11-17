# Budgets for Direct Provider Calls

Griptape Cloud enforces spending budgets on every call that goes through the Griptape proxy. A node that calls a model provider directly, for example with the user's own OpenAI or Anthropic key, bypasses the proxy, so Cloud never sees the call. Two requests let such a node take part in budgets anyway:

- `BudgetAccessRequest`, sent **before** the call, asks Cloud whether the call fits the budgets of the open project.
- `ReportUsageRequest`, sent **after** the call, tells Cloud what the call cost, so it counts against those budgets and shows in usage auditing.

The engine adds the project, workflow, workflow run, engine and session to both requests for you. Send the provider's own name for the model, such as `gpt-5-nano`, in both requests, so a refusal and the spend it would have been are grouped under the same model in usage audits. Pass the node's class name as `node_type`. Save the node's `run_attribution` when the call starts and pass that saved value as `run` on both requests, as the examples below do. `node_id` is for an opaque id; never pass the node's name, which the user types and may not want shared.

## Before the call: `BudgetAccessRequest`

```python
from griptape_nodes.retained_mode.events.budget_events import (
    BudgetAccessRequest,
    BudgetAccessResultFailure,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

# Saved now, while the node is executing for its run, and passed to both requests.
run = self.run_attribution
check = await GriptapeNodes.ahandle_request(
    BudgetAccessRequest(
        model="gpt-5-nano",
        estimated_cost_micro_usd=40_000,
        node_type=type(self).__name__,
        run=run,
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
        run=run,
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

- The report is sent in the background and returns at once. It is retried briefly, then dropped with a line in the engine log. An engine that runs one workflow and exits waits for its reports before exiting, and logs any it could not send: up to 30 seconds for a workflow run from the command line, and up to 10 seconds for a group or loop set to Isolated Execution, since a loop can start one of those for each iteration.

- **Never fail a node over its report.** Ignore the result. A report with neither token counts nor a declared cost, or with both, comes back as a failure with the reason. Griptape Cloud checks everything else, such as the model or the cache counts, and a report it rejects is logged and dropped. Either way the node carries on.

- Pass the `correlation_id` from the check's success so Cloud can pair the check with the report.

## What one run cost

Each `BudgetAccessRequest` and `ReportUsageRequest` carries the id of the workflow run that made the call as the `run_id` tag, so Griptape Cloud can total what one run cost, apart from earlier runs of the same workflow. A node holds its run as `self.run_attribution` while it executes: the run's id, with the workflow and project it is running in. Save it in a local variable when the call starts, as `run = self.run_attribution`, and pass that saved value on both requests: a node stops holding its run once its execution ends, and a slow call can report after that, such as one that finishes after Cancel. The call is then counted toward that run, its workflow, and its project's budgets, even if another run or project is current by the time it reports. A report counts after the user closes the workflow too. A budget check from a closed workflow is refused, so a call that hasn't started yet doesn't run. A request that names no run is counted toward the run, workflow and project open when it arrives. A loop's iterations, a node run while another node run is in progress, and a node group set to run in a separate process on the same machine all count toward the run in progress.

A node group sent to a remote execution environment counts toward the run in progress only if its publisher library passes the run's id along. The engine that runs the published workflow takes it as `--parent-run-id`, which `LocalWorkflowExecutor.add_cli_arguments` adds, and holds it while the executor is open. The publisher library's published-workflow node reads its run's id, `self.run_attribution.run_id`, when it starts the remote run, passes it as `--parent-run-id`, and the remote side builds its executor with `from_cli_args` or passes `parent_run_id` itself. Without it, the group's spend reports under the remote engine's own run ids. The remote engine has to be a release that accepts `--parent-run-id`: an older one refuses the unknown flag and the group's run fails, so pass it only when the remote engine's version is known to take it.

The id is the run's own: the one its request's result names in `run_id`, and the one `RunQueuedEvent`, `ListRunsRequest` and `CancelRunRequest` use. A run started while another is in progress waits its turn and keeps that id. The engine announces it on `RunStartedEvent` when the run starts, so a client can learn the id as the run starts rather than when its request returns, and on `RunEndedEvent` when the run ends, with how it ended: finished, failed, or cancelled. Every run that sends a start event sends exactly one end event, including a run whose flow fails before it starts.

A client that keeps the id can ask Griptape Cloud what that run cost with `POST /api/spend/search` and the filter `tag_filters: [{"key": "run_id", "value": ["<run id>"]}]`. The response's `totals` add up every matching row, not just the page returned. The engine sends usage reports in the background, one at a time and with retries, so a total asked for right after `RunEndedEvent` arrives can be missing the run's latest reports: while Griptape Cloud is slow to answer, each report still waiting to be sent can take up to about half a minute. A report Cloud refuses, or that runs out of retries, is logged and never counted. Calls made through the Griptape Cloud proxy carry the run's id too, in their `X-Griptape-Attribution` header, so the total covers a run's proxy and direct calls together. The engine reads the run, workflow and node from the context the header is asked for in, so a node asks for it in its own task, or from a thread started in a copy of that context (`contextvars.copy_context().run`). A header asked for from a bare thread carries no run, workflow or node. A chat turn with the assistant is not part of any run, even while a workflow is running, so its header names the project, engine and session, and no run, workflow or node.
