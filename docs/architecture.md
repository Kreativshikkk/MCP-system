# MCPSystem Core — architecture boundary

## Current milestone

The current code establishes the runtime boundary plus bounded GitHub, GitLab,
Bitbucket, Jira, Linear, and YouTrack service replicas. It does not yet implement task generation,
ground truth, or evaluation scenarios.

An immutable template is provisioned once from declarative configuration. An
environment is a persisted control-plane record plus one or more isolated
clones of template service storage. A service plugin owns its schema migrations
and seed logic, while the storage backend owns transactions, physical
isolation, cloning, and connection lifecycle.

Ready environments can also be captured as immutable point-in-time snapshots.
`snapshot_environment` records the operation-log cursor and MCP surfaces, clones
every relational service store and Git data plane, and publishes the snapshot
only after its manifest is complete. `create_environment_from_snapshot` creates
an independent mutable environment from that state; its control-plane record
retains the source snapshot id. `diff_snapshots` compares relational rows by
primary key and Git refs, producing deterministic inserted/deleted/updated
records for Director ground-truth capture and Oracle verification. The snapshot
manifest is durable under the runtime data root, while PostgreSQL snapshot data
uses isolated schemas.

```text
MCPSystem
├── ControlPlaneStore
│   └── SQLiteControlPlane (reference implementation)
├── ServiceStorageBackend
│   └── SQLiteServiceStorage (reference implementation)
├── GitDataPlaneStorage
│   └── isolated bare repositories per service environment
└── PluginRegistry
    └── ServicePlugin(s)
```

SQLite is deliberately a zero-dependency reference backend for validating the
contracts and restart/isolation semantics. Plugins do not receive a
`sqlite3.Connection`; they receive the smaller `RelationalSession` protocol and
request migrations for the active storage kind. A PostgreSQL implementation can
therefore be added without changing environment lifecycle or MCP routing.

The PostgreSQL implementation uses one control-plane schema and one schema per
service instance. Template cloning creates the target schema from the pinned
plugin migrations, copies tables in foreign-key dependency order, and resets
serial/identity sequences. It never calls the plugin seed while cloning.

Services with the `git_data_plane` capability additionally receive a filesystem
root containing one bare repository per relational repository id. Template
cloning copies that object/ref state into a separate environment root. Provider
operations use a small cross-plane unit of work: SQL remains authoritative,
mutable Git refs are journaled and restored when the SQL transaction fails,
while unreachable immutable objects are left for normal Git garbage collection.

The MCP layer is split at the transport boundary. `SurfaceRegistry` defines the
tools belonging to each persisted surface, `MCPDispatcher` binds them to one
environment/service/actor tuple, and `MCPJSONRPCServer` implements lifecycle and
tools over stdio. Provider exceptions cross the dispatcher as correctable tool
errors; protocol-shape and unknown-tool failures remain JSON-RPC errors.

MCP and internal provider-contract adapters invoke operations through one audited runtime
boundary. The control plane creates a durable `running` operation record before
dispatch and completes it after provider commit or rollback. Interrupted
attempts are recovered explicitly after restart, and the runtime exposes a
read-only operation timeline for the future Inspector UI. Protocol parsing and
authentication failures remain transport concerns and are not domain events.

The first Inspector implementation is a separate read-only HTTP adapter over
runtime model APIs. It renders environment summaries and the durable operation
timeline, never queries plugin tables directly, and is not registered as an MCP
surface. Until authentication is added it is loopback-only and must remain
outside Air's sealed evaluation network.

Provider artifact views use version-pinned `InspectorProjectionAdapter`
implementations. Adapters own provider-specific reads and normalize them into
repositories, tickets, change sets, reviews, builds, and bounded Git diffs. The
Inspector frontend depends only on this projection contract, so adding GitLab,
Jira, or YouTrack does not require another provider-shaped frontend.

## Persistence invariants

- External SaaS services are never runtime storage.
- A ready environment survives MCPSystem process restarts.
- Service state is isolated between environments.
- Plugin id and version are pinned in the control plane.
- Selected MCP surfaces are part of persisted environment configuration.
- Plugin provisioning is transactional.
- Templates cannot be mutated through the MCPSystem runtime API.
- Creating an environment from a template clones state instead of re-running seed.
- Failed or interrupted provisioning remains visible as failed state.
- Control-plane and plugin schemas are versioned with migrations.
- MCP and HTTP provider operations share one durable timeline.
- Published snapshots are immutable and survive runtime restarts.
- A snapshot clone cannot observe later mutations of its source environment.

## Completed MCP service foundation

`github@0.1.0` validates the broad provider boundary with a real 20-table
service schema. `gitlab@0.1.0` adds the first multi-provider vertical slice:
groups, projects, issues and notes, real Git commits/branches, merge requests,
approvals, pipelines/jobs, releases, a 78-tool MCP surface, 78 pinned `/api/v4`
routes, and a universal Inspector adapter. `jira@0.1.0` adds independent task
tracking with issues, comments, transitions, links, boards, sprints, a 22-tool
MCP surface, and an Inspector projection.
Bitbucket, Linear, and YouTrack complete the six-provider company template. The
combined role-bound stdio MCP surface exposes 220 agent-facing tools. All
selected contracts, SQLite and PostgreSQL lifecycle tests, cross-service
workflows, and recorded live differentials pass independently.

API/MCP dispatch remains a separate layer and is not implied by persistence.
Streamable HTTP MCP, Git smart protocol/working-tree checkout, and vendor-wide
API parity are deliberate exclusions from this bounded foundation.

Next:

1. Add Director perturbation records and GT capture on top of snapshot pairs.
2. Add the mechanical-first Oracle and hermetic Air evaluation lifecycle.
3. Add snapshot retention and Git-GC interfaces when production retention
   policy requires them.
