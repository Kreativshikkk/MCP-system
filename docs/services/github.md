# GitHub service replica

## Contract provenance

- Source: `github/rest-api-description`
- Revision: `5e28810649ba41b5483753ba74f976f83856a504`
- API version: `2026-03-10`
- Selected operation catalog: `contracts/github/operations.json`

The extraction command verifies the downloaded source document's SHA-256
against `contracts/github/openapi-source.json` before regenerating the bounded
contract. This makes the pinned revision check reproducible without committing
the full upstream OpenAPI document.

OpenAPI defines the external request/response contract. The local persistence
model is normalized independently because API response objects are not database
tables.

## Persistence schema

The initial plugin schema covers:

- organizations, users, organization membership;
- repositories and collaborators;
- commits, commit parents, and branches;
- labels, issues, assignees, and comments;
- pull-request state, requested reviewers, reviews, and review comments;
- workflow runs and jobs;
- releases.

GitHub issues and pull requests share the repository issue-number namespace.
`github_issues.is_pull_request` identifies PR rows, and
`github_pull_requests` stores the PR-only extension.

## Bootstrap

`configs/templates/github-default.toml` creates only company infrastructure:

- the `acme` organization;
- Director, Team Lead, Engineer, and QA bot identities;
- organization membership and repository permissions;
- one empty `acme/product` repository;
- GitHub's standard default issue labels.

It deliberately does not create issues, pull requests, commits, CI runs, or
benchmark tasks. Those must be created later through service operations.

## Current boundary

Implemented:

- SQLite and PostgreSQL migrations;
- strict deterministic bootstrap validation;
- persisted template and environment instances;
- clone isolation and identity-sequence restoration;
- relational constraints and indexes.
- actor resolution and repository permission checks;
- organization/user reads;
- repository list/get/create/update;
- label list/create and issue label mutations;
- issue list/get/create/update with atomic repository-scoped numbering;
- issue assignee mutations;
- issue comment list/create with atomic comment counters;
- real bare-Git blob/tree/commit creation with relational commit projection;
- branch refs synchronized with relational branch state;
- pull-request list/get/create/update with the shared issue-number namespace;
- requested-reviewer add/list/remove and review create/list;
- inline pull-request review-comment create/list;
- merge state transitions that atomically close the PR issue and advance the
  base branch;
- isolated Git data-plane cloning with ref compensation when the SQL
  transaction rolls back;
- GitHub-shaped serializers and provider error types.

`merge_pull_request` accepts only an already persisted `merge_commit_sha`. It
validates that the commit belongs to the repository and references the current
base and head commits as parents. `create_commit` creates the real blobs, tree,
and commit in the environment's bare repository before recording the relational
projection. A failed operation restores changed refs; immutable unreachable
objects may remain and are safe to collect with Git GC.

The bounded surface now includes 46 MCP tools, including selected Actions
runs/jobs/logs and releases. Static contract, SQLite/PostgreSQL lifecycle, and
live differential gates have passed for the selected workflows.

Deliberate exclusions are complete GitHub permission parity, Git smart protocol
and working-tree checkout, generated models for the entire OpenAPI document,
and all endpoints outside the pinned 46-tool selection. These are not blockers
for the completed MCP service foundation and should be added only for a named
benchmark workflow.

The local HTTP middleware currently routes 38 selected REST endpoints. It
supports both root-relative and `/api/v3` paths and never proxies unsupported
routes to the external provider.
