# Durable operation log

MCPSystem records every provider-domain operation dispatched through its MCP or
HTTP adapters. The log is stored in the control plane and survives process
restarts. It is the timeline source for the future Inspector UI and benchmark
artifacts such as standup windows; it is not ground truth and does not replace
perturbation-time GT capture.

Both transports call `MCPSystem.invoke_service_operation()`. A record is
inserted as `running` before the provider transaction starts and is completed
only after that transaction either commits or rolls back:

```text
MCP  ─┐
      ├─> invoke_service_operation ─> GitHub operations ─> SQL + Git
HTTP ─┘              │
                     └─> durable operation record
```

Records contain the environment and service identity, plugin, actor, transport,
domain operation name, JSON request, JSON result or structured error, and start
and completion timestamps. An unfinished `running` record is marked
`interrupted` when MCPSystem restarts. Transport parsing failures, unknown
routes/tools, authentication failures, and invalid MCP arguments are not domain
operations and therefore are not included.

The read API is deliberately runtime-owned rather than exposed on an
agent-selected MCP surface:

```python
records = system.list_operations(environment_id, limit=100)
```

This separation is important for the hermetic evaluator: a future Inspector API
may expose the trace to benchmark authors while keeping hidden perturbation and
oracle data inaccessible to Air.

Only provider arguments are recorded. HTTP authorization headers and tokens do
not enter the operation record. Before supporting plugins whose operation
arguments can contain credentials, the shared invocation boundary must add an
explicit plugin-aware redaction policy.

The current implementation guarantees that an attempt is visible before its
provider transaction begins. Success is recorded after commit and failure after
rollback. Because the control plane and service storage may be separate
databases, there is no distributed atomic commit between provider state and the
final log status. A crash in that narrow interval leaves an `interrupted` record
rather than claiming a false success; a future outbox/reconciliation layer can
close that ambiguity if exactly-once event delivery becomes necessary.
