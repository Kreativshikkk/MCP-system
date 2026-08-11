# Live provider differential checks

`scripts/provider_differential.py` sends canonical HTTP or GraphQL requests to
an official provider and equivalent provider-shaped operations through the
audited local runtime. Real and replica identifiers are captured separately;
the comparer checks status, response subset shape, selected semantic fields and
headers only after scenario-declared normalization.

Read-only smoke scenarios exist for GitHub, GitLab, Jira, Bitbucket, Linear and
YouTrack under `contracts/differential/`. Tokens are accepted directly or from
a file and are never written to cassettes. Example:

```bash
PYTHONPATH=src .venv/bin/python scripts/provider_differential.py \
  --scenario contracts/differential/bitbucket-read-smoke.json \
  --real-url https://api.bitbucket.org/2.0 \
  --real-token-file /secure/path/bitbucket.token \
  --data-root data/company \
  --environment ENVIRONMENT_ID \
  --instance bitbucket \
  --actor engineer
```

Mutating scenarios are rejected unless the scenario declares
`safety.disposableOnly=true` and the caller passes `--confirm-disposable`.
Use unique run IDs, resources owned by a disposable test workspace, and an
explicit cleanup step. Never point mutation scenarios at production tenants.

The GitLab disposable core lifecycle is defined by
`contracts/differential/gitlab-core-live.json`. It covers project lookup,
label/issue/note creation, state transition, not-found parity, and cleanup.
`contracts/differential/gitlab-engineering-live.json` additionally covers
temporary branches and commits, repository file content, merge requests and
diffs, review notes, pipeline creation, job endpoint reachability, and cleanup.
GitLab may populate `diff_refs` asynchronously, so that field is explicitly
excluded while stable MR semantics remain compared.

The GitHub disposable core lifecycle is defined by
`contracts/differential/github-core-live.json`. GitHub does not expose issue or
pull-request deletion, so the scenario closes its trace objects instead of
pretending cleanup removed them. Repository-global labels created by repeated
runs may be removed separately after the comparison.
`contracts/differential/github-engineering-live.json` covers a temporary branch,
workflow commit, pull request, COMMENT review, Actions run/job discovery, PR
closure, and branch cleanup. Real Actions discovery uses bounded polling because
GitHub creates runs and jobs asynchronously. The local MCP job-log operation
returns captured text directly; GitHub's REST log endpoint instead redirects to
a temporary archive and is intentionally not treated as the same response shape.

Additional disposable scenarios:

- `jira-core-live.json`: issue, ADF comment, update and verification;
- `bitbucket-engineering-live.json`: canonical multipart `/src` commit, PR,
  diff, review comment, commit status, decline and branch cleanup;
- `linear-core-live.json`: GraphQL issue/comment/update/archive lifecycle;
- `youtrack-core-live.json`: issue/comment/update and command lifecycle.

Bitbucket Pipelines is reported separately when the real repository cannot be
provisioned by the vendor pipeline service. A provider `404` is not normalized
into a passing replica result.

Provider authentication flags:

- GitHub, Linear and YouTrack: default `Authorization: Bearer ...`;
- GitLab: `--token-header PRIVATE-TOKEN --token-prefix ''`;
- Bitbucket OAuth token: default bearer header;
- Jira API token: pass a base64 `email:token` value with
  `--token-prefix 'Basic '`.

Scoped Jira API tokens can instead use `Bearer` through
`https://api.atlassian.com/ex/jira/{cloudId}`. The live runner supports explicit
bounded status retries (`--retry-status`, `--retry-attempts`) for transient
gateway responses; retries are disabled by default for every provider.

Live success is reported separately from static contract conformance. Absence
of credentials remains `pending`, never `passed`.
