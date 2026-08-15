# GitLab service replica

`gitlab@0.1.0` is a bounded GitLab REST v4 core backed by isolated
SQLite or PostgreSQL service storage and the shared bare-Git data plane.

The contract is pinned to GitLab OpenAPI `19.3.0-pre`, commit
`eb75d05715acad3d0ca93f7fbc699e7736470297`. The extracted machine-readable
core contract lives in `contracts/gitlab-core-openapi.json`; contract tests
verify that every public route exists in that revision and uses a documented
response code and request-field vocabulary.

Implemented resources:

- users, groups/namespaces, memberships, and projects;
- labels, issues, assignees, and issue notes;
- repository tree/files, real commits, branches, tags, and comparisons;
- merge requests, requested reviewers, approvals, notes, and discussions;
- pipelines, jobs, traces, and commit statuses;
- releases;
- a 78-tool `gitlab_rest_v4` MCP surface and 78 `/api/v4` HTTP routes;
- live three-way merge status plus an MCP-only conflict-resolution commit
  operation that records source and target as parents;
- the provider-neutral Inspector projection, including real bounded diffs.

The API vocabulary follows GitLab conventions such as project paths, per-project
`iid`, `opened` state, source/target branches, notes, access levels, and pipeline
statuses. Contract provenance is the official GitLab REST API documentation at
`https://docs.gitlab.com/api/rest/`.

Runtime differential checks use the pinned local GitLab CE container. Known
schema/runtime deviation: GitLab CE 19.2 returns `204` with no body from
`DELETE /projects/:id/labels`, while the pinned 19.3-pre OpenAPI document lists
`200`. The replica follows the observed runtime behavior for this endpoint.

This version deliberately does not claim complete GitLab compatibility.
Integrations, invitations, Jira endpoints, administration, billing, packages,
registries, runners, webhooks, and other SaaS/enterprise features are outside
the core scope. Inline diff positions, artifacts, pagination details and the
full set of optional response fields remain explicit follow-up compatibility
work. They should be added only when a benchmark workflow needs them, while
preserving fixed-at-perturbation GT and the durable operation boundary.
