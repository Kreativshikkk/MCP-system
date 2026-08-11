# YouTrack service replica

`youtrack@0.1.0` is a bounded 20-tool YouTrack REST adapter covering users,
projects, issues, commands, comments, links, agile boards and sprints, work
items and VCS-change reads. Contract provenance is the official generated
JetBrains REST reference pinned under `contracts/youtrack/`. Administration,
knowledge-base and attachment APIs are excluded. Live differential coverage
passed against project `DEMO` for current user, issue create/read/comment/update,
and terminal `State Done` command.
