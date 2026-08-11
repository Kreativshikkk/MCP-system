# MCPSystem

MCPSystem is a local runtime for persistent, isolated replicas of services such
as GitHub, GitLab, Bitbucket, Jira, Linear, and YouTrack. External services are sources of API
contracts and optional conformance checks; they are not runtime dependencies.

The MCP service foundation milestone is complete. It provides:

- a versioned service-plugin contract;
- a persistent control plane;
- isolated per-environment service databases;
- transactional plugin migrations and seeding;
- persisted selection of MCP surfaces;
- strict TOML environment/template configuration;
- immutable templates and isolated clone-on-create environments;
- immutable point-in-time environment snapshots, independent snapshot clones,
  and structured SQL/Git snapshot diffs;
- restart and isolation guarantees covered by tests.
- a durable, transport-neutral provider operation log.

Task generation, benchmark scenarios, perturbation-time ground truth, filtering,
and the Oracle are the next benchmark-harness layer; they are intentionally
outside the completed MCP service foundation.

## Built-in service plugins

The built-in `github@0.1.0` plugin provides SQLite and
PostgreSQL schemas for the core software-company resources and a deterministic
minimal bootstrap. Its first transactional operation set covers repositories,
issues, labels, assignees, comments, relational commit/branch state, pull
requests, requested reviewers, reviews, review comments, and merge transitions.
Each environment also owns isolated local bare-Git repositories containing the
real blobs, trees, commits, and refs. Contract provenance and the current
coverage boundary are documented in `docs/services/github.md`.

The bounded `gitlab@0.1.0` core adds groups/projects, labels, issues/notes,
repository files/commits/branches/tags, merge requests/discussions/approvals,
pipelines/jobs/statuses, and releases. It preserves the same
SQLite/PostgreSQL isolation and real-Git guarantees and exposes 78 MCP tools
through `gitlab_rest_v4` plus 78 matching HTTP routes under `/api/v4`.

The bounded `jira@0.1.0` core adds users, projects, issues, comments,
workflow transitions, issue links, Scrum boards, and sprint lifecycle. Its
`jira_rest_v3` MCP surface exposes 22 tools backed by isolated relational state.

The six built-in surfaces expose 222 tools in the combined company template
through a stateful MCP
`2025-11-25` JSON-RPC stdio server. Environment, actor, and service routing are
fixed when the server starts rather than accepted from model-controlled tool
arguments. Setup and protocol details are in `docs/mcp.md`.

Agents connect directly to these local, contract-compatible MCP surfaces. The
GitHub and GitLab REST implementations remain useful for conformance testing,
but no vendor MCP process or external service is part of the runtime.

For a combined six-service environment and role-bound client config,
run `scripts/materialize_company.py` followed by
`scripts/generate_mcp_config.py`. Exact commands are in `docs/mcp.md`.

MCP and HTTP provider calls are recorded in the same persistent operation
timeline for future task inspection and standup/release artifacts. See
`docs/operation-log.md`.

The completed boundary is deliberately bounded: local agents use MCP over
stdio, and repository work uses explicit provider-shaped commit/file/branch
operations backed by real bare Git. Streamable HTTP MCP, Git smart protocol,
working-tree checkout, and complete vendor-wide API parity are excluded until a
benchmark workflow requires them.

Inspect environments and their MCP/HTTP operation timeline in the local
read-only UI:

```bash
PYTHONPATH=src .venv/bin/python scripts/inspector.py --data-root data --port 8777
```

The Inspector projects all six providers into the same author-facing model:
repositories/projects, tickets/issues, pull/merge requests, reviews/approvals,
real Git diffs, Actions/pipelines, and Jira project tickets. Built-in templates
live under `configs/templates/`.

Then open `http://127.0.0.1:8777`. The UI and its loopback-only security
boundary are documented in `docs/inspector.md`. Its Artifacts workbench uses
provider-neutral ticket/change-set/review/build projections rather than copying
the GitHub interface.

Materialize the GitHub template and one isolated PostgreSQL environment:

```bash
MCP_SYSTEM_POSTGRES_DSN=postgresql://mcp_system:mcp_system@127.0.0.1:55432/mcp_system \
  .venv/bin/python scripts/materialize_github.py
```

Run its MCP server after substituting the printed environment id:

```bash
.venv/bin/python scripts/mcp_server.py \
  --environment ENVIRONMENT_ID \
  --actor engineer \
  --postgres-dsn postgresql://mcp_system:mcp_system@127.0.0.1:55432/mcp_system
```

## Run tests

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

## PostgreSQL backend

Start the local PostgreSQL 18 instance:

```bash
docker compose up -d --wait postgres
```

Construct the runtime with persisted PostgreSQL control and service schemas:

```python
from pathlib import Path
from mcp_system import MCPSystem, PluginRegistry

registry = PluginRegistry()
# Register service plugins before opening or creating environments.

system = MCPSystem.with_postgres(
    Path("data"),
    registry,
    "postgresql://mcp_system:mcp_system@127.0.0.1:55432/mcp_system",
)
```

Run PostgreSQL integration tests:

```bash
MCP_SYSTEM_TEST_POSTGRES_DSN=postgresql://mcp_system:mcp_system@127.0.0.1:55432/mcp_system \
  .venv/bin/python -m unittest discover -s tests -v
```

## Declarative environment

```toml
[environment]
name = "local software company"
mcp_surfaces = ["github_standard", "codebase"]

[[services]]
instance_id = "code_host"
plugin = "github"
version = "1.0.0"

[services.seed]
organization = "acme"
```

Template configuration uses the same `services` array with a `[template]`
header containing `id`, `name`, `version`, and `mcp_surfaces`.
