# Bitbucket service replica

`bitbucket@0.1.0` is a bounded Bitbucket Cloud API 2.0 replica with 36 MCP
tools, isolated SQLite/PostgreSQL state, real bare-Git objects and Inspector
projection. The pinned official Swagger snapshot and selected operation matrix
are under `contracts/bitbucket/`. Covered workflows are repositories, commits,
files, branches, issues/comments, pull-request review/merge, Pipelines steps and
logs, and commit statuses. Live differential coverage passed against the
disposable `nikiniks2005/mcp_test` repository for identity, commit/ref, PR/diff,
review comment, commit status, decline, and cleanup. The official repository
Issue tracker is unavailable there, and Pipelines creation remains blocked by
the vendor account-service/repository association; neither result is normalized
into a false compatibility pass.
