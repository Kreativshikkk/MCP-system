# Driving MCP-system from a harness

An external harness (AABench) uses MCP-system as the world its generated
agents live in: it seeds a repository, watches what the agents do, injects
defects through the admin plane, and snapshots the result. This document
records the primitives that exist for that, and why each one is shaped the way
it is.

The harness talks to `MCPSystem` **in process** — it imports the package. The
agents talk to the same environment over stdio MCP, in their own processes,
one process per (environment, actor). Nothing about the admin plane is
reachable from a tool call.

## Environment lifecycle

```python
environment = system.create_environment_from_template("aabench_gitlab_jira")
...
system.delete_environment(environment.id)
```

`delete_environment` removes the control-plane rows, the service databases and
the bare Git repositories. It is not a nicety: a harness creates a scratch
environment per filtered candidate and per snapshot self-test, so a system
that only accumulates worlds fills the disk inside one run.

`configs/templates/aabench-gitlab-jira.toml` provisions the two services the
harness uses. The repository is created empty; the harness writes the seed
tree itself, because ground truth has to be computed from a tree the harness
authored.

## Freeze

```python
system.freeze_environment(env_id)
blob = system.export_environment(env_id)
system.unfreeze_environment(env_id)
```

While frozen, `invoke_service_operation` raises `EnvironmentFrozenError`. The
flag is a control-plane column, not an in-memory lock, because the writers to
be blocked are agents in other processes.

The admin plane passes `allow_frozen=True` to write during a freeze. The MCP
dispatcher never passes it, so there is no agent-reachable path to a frozen
world — the same "absent from the surface" rule that keeps `update_pipeline`
admin-only.

## The mutation log is a watermark

Every operation carries a `seq` that is monotonic within its environment, so:

```python
mark = system.operation_watermark(env_id)
...                                  # let an agent work
fresh = system.list_operations(env_id, since_seq=mark, actor="engineer")
```

`since_seq` and `actor` are what make the log usable for "has this agent done
anything lately" — the signal a harness uses to detect a stuck session without
asking a model. `tool_call_id` carries the MCP request id when the operation
came from a tool call, so a world mutation can be traced back to the exact
call that caused it.

Ordering by `started_at` with a random-uuid tiebreak, which is what existed
before, is not a watermark: two operations in the same clock tick have no
defined order and a caller cannot resume from a point in the stream.

## Byte-stable export and import

`snapshot_environment` clones an environment on disk. It is fast, but its
bytes are database pages and a manifest stamped with the wall clock, so two
exports of the same logical state differ — a caller that treats a digest as
the world's identity cannot use it.

`export_environment` writes a canonical *logical* document instead: relational
rows sorted by primary key, plus every Git object keyed by its sha, with no
timestamps in the envelope. It guarantees

```
export(import(export(x))) == export(x)
```

`import_environment` rebuilds it into a new environment (a new id — callers
that need stable identity keep their own mapping, which is far cheaper than
teaching every storage backend to restore in place).

Note what is *not* required: deterministic commit shas. Commit objects still
embed the wall clock, and that is fine, because a harness compares tree
content it hashes itself, not commit ids.

## Git reads the harness depends on

On `BareGitRepository`:

- `read_tree_contents(ref) -> {path: bytes}` — the whole tree with content.
  Merkle-hashing a tree needs bytes, and one `git show` per blob costs a
  process per file.
- `log(to_ref=, from_ref=, merges_only=)` — a real range walk, which is what
  produces a release's merge set. `list_commits` on the GitLab surface returns
  every commit in the project and ignores the range.
- `update_tag` / `resolve_tag` — tags as real `refs/tags/*`. A tag that is only
  a SQL row is invisible to every git-level read, including the exporter.

## CI verdicts are admin-only

`gitlab set_commit_status` and `bitbucket create_commit_status` were on the
agent surfaces. An engineer agent could post `state="success"` and forge a
green build, which makes every downstream "did CI pass" judgement worthless.
They are now withheld from the MCP surfaces, exactly like `update_pipeline`,
and `tests/test_gitlab_contract.py` / `tests/test_bitbucket_contract.py` fail
if they come back.

They remain implemented, and remain on the HTTP provider-emulation routers —
that surface exists to diff against the real providers, not to serve agents.

Still open: `merge_merge_request` gates on approvals only, never on pipeline
status, so "merge only when CI is green" is not enforced by the replica. The
harness performs merges itself through the admin plane, so this does not block
Phase 1, but an agent-driven merge flow would need it.
