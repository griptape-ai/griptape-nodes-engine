# Budgets for Direct Provider Calls

Griptape Cloud enforces spending budgets on every call that goes through the Griptape proxy. A node that calls a model provider directly, for example with the user's own OpenAI or Anthropic key, bypasses the proxy, so Cloud never sees the call. Two requests let such a node take part in budgets anyway:

- `BudgetAccessRequest`, sent **before** the call, asks Cloud whether the call fits the budgets of the open project.
- `ReportUsageRequest`, sent **after** the call, tells Cloud what the call cost, so it counts against those budgets and shows in usage auditing.

The engine adds the project, workflow, engine and session to both requests for you. Pass the node's class name as `node_type`. `node_id` is for an opaque id; never pass the node's name, which the user types and may not want shared.

## Before the call: `BudgetAccessRequest`

```python
from griptape_nodes.retained_mode.events.budget_events import (
    BudgetAccessRequest,
    BudgetAccessResultFailure,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

check = GriptapeNodes.handle_request(
    BudgetAccessRequest(
        model_id="gtc_claude_opus_4_7",
        estimated_cost_micro_usd=40_000,
        node_type=type(self).__name__,
    )
)
if isinstance(check, BudgetAccessResultFailure):
    raise check.exception
```

- A **failure** means a budget refused the call. Do not make it. Raise `check.exception`: the run stops with a message naming the node and every budget that refused, and the editor shows its "Run blocked" bar.
- A **success** means go ahead. `checked` is `False` when Cloud could not be reached or no Griptape Cloud credential is set; the call is still cleared. A network problem must never stop work.
- `estimated_cost_micro_usd` is optional. Without it, only a budget with no headroom left refuses the call.

## After the call: `ReportUsageRequest`

```python
from griptape_nodes.retained_mode.events.budget_events import ReportUsageRequest

GriptapeNodes.handle_request(
    ReportUsageRequest(
        declared_cost_micro_usd=12_345,
        provider="anthropic",
        model="claude-sonnet-5",
        activity_type="chat_completion",
        node_type=type(self).__name__,
        correlation_id=check.correlation_id,
    )
)
```

- The report is sent in the background and returns at once. It is retried briefly, then dropped with a line in the engine log.
- **Never fail a node over its report.** Ignore the result.
- Pass the `correlation_id` from the check's success so Cloud can pair the check with the report.
- Costs are in millionths of a US dollar: `1_000_000` is $1.00.
