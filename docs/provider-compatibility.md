# Provider compatibility

Compatibility has two independent gates:

1. **Contract** — the selected tool surface, required arguments, operation kind,
   permissions, failures, persistence and lifecycle behavior are covered by the
   pinned contract and automated tests.
2. **Live differential** — the same canonical scenario has been executed against
   an official provider and the local implementation, with normalized semantic
   responses compared.

`pending` never means passed. Live mutation scenarios must use disposable tenants.

| Provider | Contract tests | Lifecycle / negative tests | Live differential |
| --- | --- | --- | --- |
| GitHub | passed for 46-tool bounded surface at pinned REST revision `5e288106…` / API `2026-03-10` | passed on SQLite and PostgreSQL | passed against `Kreativshikkk/mcp-system-test`: identity, repository, issue lifecycle, branch/workflow commit, PR, COMMENT review, Actions run/job, close and branch cleanup. MCP job-log text is adapter-shaped; official REST log download is a `302` archive redirect and is not claimed as byte-identical |
| GitLab | passed | passed | passed against GitLab CE 19.2.0: identity, issue lifecycle, temporary commits/branches, file content, MR/diff/review note, pipeline creation and cleanup. Exact runner/job-state parity remains pending |
| Jira | passed | passed | passed against Jira Cloud project `SCRUM`: identity, create/read issue, ADF comment, update and verification; scoped Atlassian gateway uses bounded retry for observed transient `401` responses |
| Bitbucket | passed against pinned Swagger selection | passed | passed against `nikiniks2005/mcp_test`: identity, multipart commit/ref, PR/diff/review comment, commit status, decline and branch cleanup. Repository Issue tracker API is unavailable (`404`). Pipelines creation is blocked by vendor `account-service.repository.not-found` despite repository API access |
| Linear | passed against pinned GraphQL operation matrix | passed | passed against team `MCP`: viewer, issue create/read, comment, update and archive |
| YouTrack | passed against pinned REST operation matrix | passed | passed against project `DEMO`: current user, issue create/read, comment, update and terminal `State Done` command |

The live runner and provider-specific authentication examples are documented in
[`live-differential.md`](live-differential.md). Differential cassettes are
diagnostic artifacts; they do not replace the pinned contracts or automated
tests.
