# MCP server

MCPSystem exposes persisted service replicas through a transport-independent
dispatcher and a stateful JSON-RPC stdio server. The implementation targets MCP
revision `2025-11-25` and also negotiates `2025-06-18` for older clients. The
wire behavior follows the official [lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle),
[tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools),
and [stdio transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
specifications.

## Routing and isolation

The server process is bound to exactly one:

- environment id;
- actor login;
- service instance for each selected MCP surface.

Bindings are inferred when the environment has exactly one instance of the
surface's plugin. An explicit instance override is needed only when an
environment contains multiple instances of that provider. This keeps the
launcher independent of the set of built-in services.

The model cannot override the actor or environment in tool arguments. Tool
discovery is derived from the environment's persisted `mcp_surfaces`; a surface
that is unregistered, unbound, or bound to the wrong plugin prevents server
startup instead of silently exposing a partial tool set.

`github_rest_v3` exposes 46 tools covering identities,
organizations, repositories, real Git commits and branches, issues, labels,
assignees, comments, pull requests, reviewers, reviews, inline review comments,
merge transitions, Actions runs/jobs/logs, and releases.

`gitlab_rest_v4` exposes 78 tools across repository, issue, merge request,
review, CI/CD, and release workflows. `jira_rest_v3` exposes 22 tools covering
projects, issues, comments, transitions, issue links, boards, and sprints.

## Running over stdio

For the default PostgreSQL runtime:

```bash
.venv/bin/python scripts/mcp_server.py \
  --data-root data \
  --environment ENVIRONMENT_ID \
  --actor engineer \
  --github-instance github \
  --postgres-dsn postgresql://mcp_system:mcp_system@127.0.0.1:55432/mcp_system
```

Omit `--postgres-dsn` to use the SQLite control plane and service databases
under `--data-root`. Stdout is reserved exclusively for newline-delimited MCP
JSON-RPC messages.

## Multi-service company launcher

Materialize one environment containing GitHub, GitLab, Bitbucket, Jira, Linear,
and YouTrack (222 tools total):

```bash
PYTHONPATH=src .venv/bin/python scripts/materialize_company.py
```

The command prints the environment id and a follow-up command that emits a
conventional `mcpServers` JSON object with role-bound entries for `director`,
`lead`, `engineer`, and `qa`:

```bash
PYTHONPATH=src .venv/bin/python scripts/generate_mcp_config.py \
  --data-root data \
  --environment ENVIRONMENT_ID
```

PostgreSQL credentials are deliberately not written into the JSON. Set
`MCP_SYSTEM_POSTGRES_DSN` in the MCP client's environment when using that
backend. A client acting in one role should install only that role's entry.

## Result and error contract

Successful tool calls return both:

- a JSON text content block for compatibility;
- `structuredContent` as `{ "result": ... }`.

Provider validation, permission, conflict, and not-found failures are tool
results with `isError: true`, so an agent can inspect and correct the call.
Malformed protocol requests and unknown tools are JSON-RPC errors.

## Deliberate transport boundary

Agents use the stdio MCP server. Streamable HTTP MCP is deliberately excluded
from the completed service foundation because the local sealed harness does not
require it. Provider REST routers are retained for contract conformance tests;
they are not runtime dependencies. Add another MCP transport only when a
benchmark client has a concrete requirement for it.
