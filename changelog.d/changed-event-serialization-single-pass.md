- Serializing an event now walks its payload once instead of twice, cutting serialization cost —
  most noticeably on events with large parameter values.
