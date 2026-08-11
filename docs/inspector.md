# MCPSystem Inspector

Inspector is a read-only local UI for benchmark authors. It visualizes persisted
environments and the durable provider-operation timeline without querying
plugin databases directly.

Run it against the default SQLite control plane:

```bash
PYTHONPATH=src .venv/bin/python scripts/inspector.py \
  --data-root data \
  --port 8777
```

Or against PostgreSQL:

```bash
MCP_SYSTEM_POSTGRES_DSN=postgresql://mcp_system:mcp_system@127.0.0.1:55432/mcp_system \
  PYTHONPATH=src .venv/bin/python scripts/inspector.py --port 8777
```

Open `http://127.0.0.1:8777`. The initial UI provides:

- environment selection and lifecycle status;
- operation totals, success rate, failures, and active actors;
- a newest-first timeline shared by MCP and HTTP;
- search and transport, status, and actor filters;
- request, result, and structured-error details;
- provider-neutral repository, ticket, change-set, review, build, and diff views;
- responsive desktop and compact layouts.

The HTTP surface is intentionally read-only. Its current API is:

```text
GET /api/health
GET /api/environments
GET /api/environments/{environment_id}/operations?limit=500
GET /api/environments/{environment_id}/workbench
```

The UI calls runtime model APIs such as `list_environments()` and
`list_operations()`; it never reads control-plane or plugin tables itself. This
keeps SQLite and PostgreSQL behavior aligned and preserves the plugin boundary.

## Universal projections

Workbench does not reproduce a provider UI. An `InspectorProjectionAdapter`
maps a pinned plugin version into a small shared vocabulary:

```text
service projection
└── repositories
    ├── tickets + comments
    ├── change sets + reviews + unified diff
    └── builds
```

The built-in adapters map GitHub Issues/Pull Requests/Actions and GitLab
Issues/Merge Requests/Pipelines to that same shape. Future Jira and YouTrack
adapters can provide ticket-only projects. Provider table names and response
objects never leak into the UI.

Diffs are generated from the real isolated Git data plane and bounded to avoid
unlimited Inspector responses. Reading a projection is not a provider operation
and does not add noise to the durable operation log.

## Security boundary

Operation requests and results can contain repository or task content. The
current server has no authentication and therefore refuses non-loopback binds.
It must not run inside Air's sealed evaluation network. A later authenticated
Inspector API can separate author-visible trace, perturbation, GT, and Oracle
views from the provider interfaces available to Air.
