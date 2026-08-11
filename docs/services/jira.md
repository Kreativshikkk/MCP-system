# Jira service boundary

`jira@0.1.0` is a bounded, deterministic replica of Jira Cloud concepts used by
the benchmark. Jira Cloud Platform and Software OpenAPI documents are pinned at
upstream version `1.8516.75`; checksums and the selected endpoint matrix live in
`contracts/jira/`.

The initial boundary contains users, projects and membership, issues, labels,
comments, priorities, assignees, three workflow states with discoverable
transitions, issue links, Scrum boards, sprint lifecycle, and sprint issue
assignment. It deliberately excludes attachments, custom fields, permissions
schemes, service management, automation rules, and arbitrary JQL.

The MCP surface is `jira_rest_v3`. Environment and actor identity are fixed at
server startup. Jira is implemented locally and never forwards to Atlassian.
The built-in seed is `configs/templates/jira-default.toml`.
