- A `DatePicker` trait gives a `str` parameter a calendar picker in the editor. The value is an
  ISO 8601 local date such as `2026-10-14`, or `2026-10-14T09:30` with `include_time=True`, so a
  node can read it with `date.fromisoformat`. Optional `min` and `max` bound the allowed dates.
  [#5890](https://github.com/griptape-ai/griptape-nodes-engine/issues/5890)
