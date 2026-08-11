# Linear service replica

`linear@0.1.0` is a bounded 20-tool adapter to Linear's documented GraphQL
vocabulary: viewer/users, teams and workflow states, labels, projects, cycles,
issues, comments and issue relations. The official documentation snapshot and
explicit query/mutation selection are pinned under `contracts/linear/`. It does
not claim the complete Linear schema. Live differential coverage passed against
team `MCP` for viewer identity and issue create/read/comment/update/archive.
